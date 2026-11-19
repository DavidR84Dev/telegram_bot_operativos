"""Cliente XML-RPC de SOLO LECTURA para Odoo.

Expone únicamente ``search_read``. No hay ``write``, ``create`` ni ``call``:
este bot no modifica datos en Odoo (el abordaje lo confirma la página web).
"""
from __future__ import annotations

import http.client
import threading
import xmlrpc.client
from typing import Any
from urllib.parse import urlparse

from .config import get_settings


class OdooClientError(Exception):
    """Error seguro para registrar y mostrar sin detalles internos."""


class _TimeoutHTTPSTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout: int):
        super().__init__()
        self.timeout = timeout

    def make_connection(self, host: str):  # type: ignore[override]  # firma más estricta a propósito: sin tuplas de certificados
        return http.client.HTTPSConnection(host, timeout=self.timeout)


class _TimeoutHTTPTransport(xmlrpc.client.Transport):
    def __init__(self, timeout: int):
        super().__init__()
        self.timeout = timeout

    def make_connection(self, host: str):  # type: ignore[override]  # firma más estricta a propósito: sin tuplas de certificados
        return http.client.HTTPConnection(host, timeout=self.timeout)


class OdooClient:
    def __init__(self) -> None:
        settings = get_settings()
        url = settings.odoo_url.rstrip("/")
        timeout = settings.rpc_timeout_seconds
        transport = (
            _TimeoutHTTPSTransport(timeout)
            if urlparse(url).scheme == "https"
            else _TimeoutHTTPTransport(timeout)
        )
        self._common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", transport=transport, allow_none=True)
        self._objects = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", transport=transport, allow_none=True)
        self._db = settings.odoo_db
        self._username = settings.odoo_username
        self._password = settings.odoo_password.get_secret_value()
        self._uid: int | None = None
        # ServerProxy/Transport no son seguros entre hilos: las llamadas se serializan.
        self._lock = threading.Lock()

    def _authenticate(self) -> int:
        if self._uid:
            return self._uid
        try:
            uid = self._common.authenticate(self._db, self._username, self._password, {})
        except (OSError, xmlrpc.client.Error) as exc:
            raise OdooClientError("No fue posible conectar con Odoo") from exc
        if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
            raise OdooClientError("No fue posible autenticar el usuario técnico de Odoo")
        self._uid = uid
        return uid

    def search_read(self, model: str, domain: list[Any], fields: list[str], limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            try:
                uid = self._authenticate()
                rows = self._objects.execute_kw(
                    self._db, uid, self._password, model, "search_read",
                    [domain], {"fields": fields, "limit": limit},
                )
            except (OSError, xmlrpc.client.Error) as exc:
                raise OdooClientError(f"La consulta a Odoo no pudo completarse ({model})") from exc
        return list(rows)  # type: ignore[arg-type]  # xmlrpc.client tipa execute_kw como genérico; search_read siempre devuelve una lista de dicts
