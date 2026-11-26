"""Cliente de la API REST de Odoo (módulo ``viacotur_api``) para las tablas maestras.

* Autenticación: ``POST /api/authenticate`` {login, password} -> JWT que vence a
  las 24 h. Las credenciales salen de ``.env`` (``ODOO_API_LOGIN``/``ODOO_API_PASSWORD``).
  El token se reutiliza mientras sea válido y se pide uno nuevo **antes** de que
  venza (claim ``exp`` del JWT menos ``REFRESH_MARGIN``; si el token no trae
  ``exp`` se asume 24 h desde su emisión). Si aun así la API responde 401
  (token revocado, reloj desfasado), se renueva una vez y se repite la petición.
* Paginación: ``?page=N&page_size=M&company_id=C``; respuesta ``{"total", "items"}``.
  Varios endpoints responden **404 "Informacion no encontrada"** cuando no hay
  datos: se trata como lista vacía.
* Resiliencia: reintentos con backoff exponencial (tenacity) ante errores de red,
  timeouts y 5xx/429. Los 4xx restantes no se reintentan (son errores de
  configuración o permisos que un reintento no arregla).
"""
from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any, Iterator

import httpx
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

logger = logging.getLogger(__name__)


class OdooApiError(Exception):
    """Error de la API REST (mensaje seguro: sin credenciales ni token)."""


class _RetryableHttpError(OdooApiError):
    pass


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, (httpx.TransportError, _RetryableHttpError))


def is_retryable_error(exc: BaseException) -> bool:
    """True si el fallo es de red o 5xx/429 (un reintento puede resolverlo); False si es definitivo."""
    return _is_retryable(exc)


_retry = retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(initial=1, max=30),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)


TOKEN_TTL_SECONDS = 24 * 60 * 60  # vigencia fija del token en base_native_api_jwt
REFRESH_MARGIN_SECONDS = 10 * 60


def _jwt_exp(token: str) -> float | None:
    """Lee el claim ``exp`` del JWT sin verificar la firma (solo para saber cuándo renovar)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return float(exp) if exp is not None else None
    except (IndexError, ValueError, TypeError, AttributeError):
        return None


class OdooRestClient:
    def __init__(
        self,
        base_url: str,
        login: str,
        password: str,
        timeout_seconds: float = 60,
        transport: httpx.BaseTransport | None = None,
        clock: Any = time.time,
    ) -> None:
        self._login = login
        self._password = password
        self._token: str | None = None
        self._token_refresh_at = 0.0
        self._clock = clock
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds, connect=10),
            headers={"Accept": "application/json", "User-Agent": "telegram-bot-operativos/preop-sync"},
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> OdooRestClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ── autenticación ──────────────────────────────────────────────────────
    @_retry
    def _authenticate(self) -> str:
        response = self._http.post("/api/authenticate", json={"login": self._login, "password": self._password})
        if response.status_code >= 500 or response.status_code == 429:
            raise _RetryableHttpError(f"/api/authenticate respondió {response.status_code}")
        if response.status_code != 200:
            raise OdooApiError(f"Autenticación rechazada por la API ({response.status_code}); revisar ODOO_API_LOGIN")
        token = response.json().get("token")
        if not token:
            raise OdooApiError("La API no devolvió token")
        now = self._clock()
        expires_at = _jwt_exp(token) or now + TOKEN_TTL_SECONDS
        if expires_at <= now:  # reloj desfasado respecto a Odoo: se confía en la vigencia nominal
            expires_at = now + TOKEN_TTL_SECONDS
        # Se renueva REFRESH_MARGIN antes del vencimiento (nunca antes de la mitad de su vida).
        self._token_refresh_at = max(expires_at - REFRESH_MARGIN_SECONDS, now + (expires_at - now) / 2)
        self._token = token
        logger.info("Token de la API REST obtenido; se renovará en %.0f min", (self._token_refresh_at - now) / 60)
        return token

    def _valid_token(self) -> str:
        if self._token is None or self._clock() >= self._token_refresh_at:
            return self._authenticate()
        return self._token

    # ── lectura paginada ───────────────────────────────────────────────────
    @_retry
    def _get_page(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        token = self._valid_token()
        response = self._http.get(path, params=params, headers={"Authorization": f"Bearer {token}"})
        if response.status_code == 401:
            # Token vencido o revocado: se renueva una vez y se repite la petición.
            token = self._authenticate()
            response = self._http.get(path, params=params, headers={"Authorization": f"Bearer {token}"})
        if response.status_code == 404:
            # Solo el 404 JSON de la API ({"detail": ...}) significa "sin datos". Un 404 HTML
            # (ruta inexistente, módulo no instalado) es un error: tratarlo como vacío
            # desactivaría todas las filas maestras.
            if "application/json" in response.headers.get("content-type", ""):
                return {"total": 0, "items": []}
            raise OdooApiError(f"{path} no existe en el servidor (404); ¿módulo viacotur_api instalado?")
        if response.status_code >= 500 or response.status_code == 429:
            raise _RetryableHttpError(f"{path} respondió {response.status_code}")
        if response.status_code >= 400:
            raise OdooApiError(f"{path} respondió {response.status_code}: {response.text[:200]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise OdooApiError(f"{path} no devolvió JSON") from exc
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise OdooApiError(f"{path}: respuesta sin 'items'")
        return data

    # ── escritura ──────────────────────────────────────────────────────────
    @_retry
    def post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST con cuerpo JSON. Solo para endpoints idempotentes (se reintenta ante 5xx/red)."""
        return self.post_json_once(path, body)

    def post_json_once(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST sin reintento automático (endpoints que crean registros: reintenta quien llama).

        Un 5xx/429 lanza ``_RetryableHttpError``, un error de red ``httpx.TransportError`` (ambos
        reintentables, ver ``is_retryable_error``) y un 4xx ``OdooApiError`` (definitivo).
        """
        token = self._valid_token()
        response = self._http.post(path, json=body, headers={"Authorization": f"Bearer {token}"})
        if response.status_code == 401:
            token = self._authenticate()
            response = self._http.post(path, json=body, headers={"Authorization": f"Bearer {token}"})
        if response.status_code >= 500 or response.status_code == 429:
            raise _RetryableHttpError(f"{path} respondió {response.status_code}: {response.text[:300]}")
        if response.status_code >= 400:
            raise OdooApiError(f"{path} respondió {response.status_code}: {response.text[:200]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise OdooApiError(f"{path} no devolvió JSON") from exc
        return data if isinstance(data, dict) else {"response": data}

    def iter_items(self, path: str, company_id: int, page_size: int = 500) -> Iterator[dict[str, Any]]:
        """Recorre todas las páginas de ``path`` para una compañía."""
        page = 1
        seen = 0
        while True:
            data = self._get_page(path, {"page": page, "page_size": page_size, "company_id": company_id})
            items = data["items"]
            yield from items
            seen += len(items)
            total = int(data.get("total") or 0)
            if not items or len(items) < page_size or seen >= total:
                return
            page += 1

    def fetch_all(self, path: str, company_id: int, page_size: int = 500) -> list[dict[str, Any]]:
        return list(self.iter_items(path, company_id, page_size))
