"""Conector XML-RPC de ESCRITURA a Odoo para el preoperacional.

Se mantiene separado de ``app.odoo_client.OdooClient`` (solo lectura, usado por
el validador de QR) para que el permiso de escritura quede acotado a un único
método: ``create_preoperacional``.

Flujo de ``create_preoperacional``:

1. ``common.authenticate`` (uid en caché; se renueva si Odoo lo rechaza).
2. Idempotencia: busca un envío previo del mismo formulario
   a. por external id ``telegram_bot_operativos.preop_<uuid>`` (``ir.model.data``);
   b. si hubo un intento anterior con resultado incierto (timeout tras enviar),
      por llave natural (empleado, vehículo, odómetro, creado después del envío).
3. ``object.execute_kw(..., 'preoperacional', 'create', [vals])``.
4. Registra el external id (si el usuario técnico tiene permiso; si no, sigue).
5. Lee ``name`` (consecutivo PRE-xxxx) para mostrarlo al usuario.

Errores:

* ``OdooTransientError``: red, timeout, 5xx, bloqueo/serialización en BD,
  autenticación caída. Se reintenta (tenacity dentro de la llamada y backoff
  entre ciclos del worker).
* ``OdooPermanentError``: Odoo rechazó los datos (ValidationError, UserError,
  campo inexistente...). Reintentar no sirve: el registro pasa a ``fallido``.
"""
from __future__ import annotations

import logging
import socket
import threading
import xmlrpc.client
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from tenacity import (
    Retrying,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from ..odoo_client import _TimeoutHTTPSTransport, _TimeoutHTTPTransport
from .checklist import CHECKLIST_BY_KEY, MEASURES_BY_KEY
from .models import Preoperacional

logger = logging.getLogger(__name__)

ODOO_MODEL = "preoperacional"
XMLID_MODULE = "telegram_bot_operativos"

# Fallos de Odoo que dependen del momento (concurrencia), no de los datos.
_TRANSIENT_FAULT_MARKERS = (
    "could not serialize access",
    "SerializationFailure",
    "deadlock detected",
    "LockNotAvailable",
    "could not obtain lock",
    "Session expired",
    "AccessDenied",
)


class OdooWriteError(Exception):
    """Error al escribir en Odoo (mensaje seguro para error_log: sin credenciales)."""


class OdooTransientError(OdooWriteError):
    pass


class OdooPermanentError(OdooWriteError):
    pass


def _classify_fault(fault: xmlrpc.client.Fault) -> OdooWriteError:
    text = str(fault.faultString or "")
    # La última línea del traceback de Odoo es el mensaje útil (p. ej. "ValidationError: ...").
    summary = text.strip().splitlines()[-1][:500] if text.strip() else f"Fault {fault.faultCode}"
    if any(marker in text for marker in _TRANSIENT_FAULT_MARKERS):
        return OdooTransientError(summary)
    return OdooPermanentError(summary)


def _odoo_datetime(value: datetime) -> str:
    """Formato de dominio de Odoo: UTC naive 'YYYY-MM-DD HH:MM:SS'."""
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    return value.strftime("%Y-%m-%d %H:%M:%S")


def build_odoo_values(preop: Preoperacional, boss_odoo_id: int | None = None) -> dict[str, Any]:
    """Traduce el registro local a ``vals`` del modelo ``preoperacional`` de Odoo.

    Solo se envían campos del catálogo (checklist/llantas): ``extra`` se queda en
    la BD local, porque un campo desconocido haría fallar el ``create`` en Odoo.
    """
    vals: dict[str, Any] = {
        "employee_id": preop.empleado.odoo_id,
        "vehicle_id": preop.vehiculo.odoo_id,
        "company_id": preop.empleado.company_odoo_id,
        "odometer_actual": float(preop.kilometraje),
        "ref_iterna": preop.numero_interno or preop.vehiculo.numero_interno or False,
    }
    if preop.extracto is not None:
        vals["extracto_id"] = preop.extracto.odoo_id
    if preop.contrato_odoo_id:
        vals["contrato_id"] = preop.contrato_odoo_id
    if boss_odoo_id:
        vals["immediate_boss_id"] = boss_odoo_id
    if preop.observaciones:
        vals["observaciones"] = preop.observaciones
    vals.update({key: value for key, value in preop.checklist.items() if key in CHECKLIST_BY_KEY})
    vals.update({key: float(value) for key, value in preop.llantas.items() if key in MEASURES_BY_KEY})
    return vals


class OdooWriter:
    def __init__(self, url: str, db: str, username: str, password: str, timeout_seconds: int = 30) -> None:
        url = url.rstrip("/")
        transport_cls = _TimeoutHTTPSTransport if urlparse(url).scheme == "https" else _TimeoutHTTPTransport
        self._common = xmlrpc.client.ServerProxy(
            f"{url}/xmlrpc/2/common", transport=transport_cls(timeout_seconds), allow_none=True)
        self._objects = xmlrpc.client.ServerProxy(
            f"{url}/xmlrpc/2/object", transport=transport_cls(timeout_seconds), allow_none=True)
        self._db = db
        self._username = username
        self._password = password
        self._uid: int | None = None
        self._xmlid_allowed = True
        # ServerProxy no es seguro entre hilos.
        self._lock = threading.Lock()

    # ── bajo nivel ─────────────────────────────────────────────────────────
    def _authenticate(self) -> int:
        if self._uid:
            return self._uid
        try:
            uid = self._common.authenticate(self._db, self._username, self._password, {})
        except (OSError, socket.timeout, xmlrpc.client.ProtocolError) as exc:
            raise OdooTransientError(f"Sin conexión con Odoo: {type(exc).__name__}") from exc
        except xmlrpc.client.Fault as exc:
            raise _classify_fault(exc) from exc
        if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
            # Credenciales inválidas: no depende del registro. Se trata como transitorio para no
            # marcar como fallidos todos los formularios mientras se corrige el .env.
            raise OdooTransientError("Odoo rechazó las credenciales del usuario de escritura")
        self._uid = uid
        return uid

    def _execute(self, model: str, method: str, args: list[Any], kwargs: dict[str, Any] | None = None) -> Any:
        uid = self._authenticate()
        try:
            return self._objects.execute_kw(self._db, uid, self._password, model, method, args, kwargs or {})
        except (OSError, socket.timeout) as exc:
            raise OdooTransientError(f"Sin respuesta de Odoo ({model}.{method}): {type(exc).__name__}") from exc
        except xmlrpc.client.ProtocolError as exc:
            if exc.errcode in (401, 403):
                self._uid = None
            raise OdooTransientError(f"Odoo respondió HTTP {exc.errcode} ({model}.{method})") from exc
        except xmlrpc.client.Fault as exc:
            error = _classify_fault(exc)
            if "AccessDenied" in str(exc.faultString):
                self._uid = None  # contraseña/API key cambiada: re-autenticar en el siguiente intento
            raise error from exc

    # ── idempotencia ───────────────────────────────────────────────────────
    @staticmethod
    def xmlid_name(external_ref: str) -> str:
        return "preop_" + external_ref.replace("-", "")

    def _find_by_xmlid(self, external_ref: str) -> int | None:
        rows = self._execute("ir.model.data", "search_read",
                             [[("module", "=", XMLID_MODULE), ("name", "=", self.xmlid_name(external_ref)),
                               ("model", "=", ODOO_MODEL)]],
                             {"fields": ["res_id"], "limit": 1})
        return int(rows[0]["res_id"]) if rows else None

    def _find_by_natural_key(self, vals: dict[str, Any], submitted_at: datetime) -> int | None:
        since = submitted_at - timedelta(minutes=2)
        ids = self._execute(ODOO_MODEL, "search", [[
            ("employee_id", "=", vals["employee_id"]),
            ("vehicle_id", "=", vals["vehicle_id"]),
            ("odometer_actual", "=", vals["odometer_actual"]),
            ("create_date", ">=", _odoo_datetime(since)),
        ]], {"limit": 1, "order": "id asc"})
        return int(ids[0]) if ids else None

    def _register_xmlid(self, external_ref: str, record_id: int) -> None:
        if not self._xmlid_allowed:
            return
        try:
            self._execute("ir.model.data", "create", [{
                "module": XMLID_MODULE, "name": self.xmlid_name(external_ref),
                "model": ODOO_MODEL, "res_id": record_id, "noupdate": True,
            }])
        except OdooPermanentError as exc:
            # El usuario técnico no administra ir.model.data: la deduplicación seguirá por llave natural.
            self._xmlid_allowed = False
            logger.warning("No se pudo registrar el external id del preoperacional (%s); "
                           "se usará solo la llave natural para deduplicar", exc)

    # ── API pública ────────────────────────────────────────────────────────
    def create_preoperacional(self, external_ref: str, vals: dict[str, Any], submitted_at: datetime,
                              previous_attempt_uncertain: bool) -> tuple[int, str | None, bool]:
        """Crea el preoperacional en Odoo. Devuelve (odoo_id, name, ya_existía).

        Reintenta errores transitorios (3 intentos, backoff con jitter). Tras un fallo
        transitorio el resultado es incierto (Odoo pudo crear el registro antes de que se
        cortara la respuesta), así que los reintentos buscan también por llave natural.
        """
        uncertain = previous_attempt_uncertain
        for attempt in Retrying(
            retry=retry_if_exception_type(OdooTransientError),
            stop=stop_after_attempt(3),
            wait=wait_exponential_jitter(initial=1, max=8),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        ):
            with attempt:
                try:
                    return self._create_once(external_ref, vals, submitted_at, uncertain)
                except OdooTransientError:
                    uncertain = True
                    raise
        raise AssertionError("inalcanzable: Retrying relanza la última excepción")  # pragma: no cover

    def _create_once(self, external_ref: str, vals: dict[str, Any], submitted_at: datetime,
                     uncertain: bool) -> tuple[int, str | None, bool]:
        with self._lock:
            existing = self._find_by_xmlid(external_ref)
            if existing is None and uncertain:
                existing = self._find_by_natural_key(vals, submitted_at)
            if existing is not None:
                return existing, self._read_name(existing), True
            record_id = int(self._execute(ODOO_MODEL, "create", [vals]))
            self._register_xmlid(external_ref, record_id)
            return record_id, self._read_name(record_id), False

    def _read_name(self, record_id: int) -> str | None:
        try:
            rows = self._execute(ODOO_MODEL, "read", [[record_id]], {"fields": ["name"]})
        except OdooWriteError:
            return None  # el registro ya existe; el nombre es solo informativo
        return (rows[0].get("name") or None) if rows else None
