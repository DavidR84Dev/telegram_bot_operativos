"""Whitelist: solo empleados con ``hr.employee.pin`` = id de Telegram y activos."""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from .odoo_client import OdooClientError

logger = logging.getLogger(__name__)

# Negativos cortos: un usuario recién autorizado no debe esperar mucho.
NEGATIVE_TTL_SECONDS = 60


class AccessControl:
    def __init__(
        self,
        lookup: Callable[[str], dict[str, Any] | None],
        ttl_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lookup = lookup
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[str, tuple[float, dict[str, Any] | None]] = {}

    def employee_for(self, telegram_user_id: str) -> dict[str, Any] | None:
        now = self._clock()
        cached = self._cache.get(telegram_user_id)
        if cached and cached[0] > now:
            return cached[1]
        try:
            employee = self._lookup(telegram_user_id)
        except OdooClientError:
            # Sin respuesta de Odoo no se concede acceso; no se cachea el fallo.
            logger.warning("No se pudo verificar la autorización de %s", telegram_user_id)
            return None
        ttl = self._ttl if employee else NEGATIVE_TTL_SECONDS
        self._cache[telegram_user_id] = (now + ttl, employee)
        return employee

    def is_authorized(self, telegram_user_id: str) -> bool:
        return self.employee_for(telegram_user_id) is not None
