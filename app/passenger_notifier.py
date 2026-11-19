"""Cliente de SOLO ENVÍO hacia ``POST /internal/notify-boarding`` del bot de pasajeros.

El validador no puede escribir al pasajero (Telegram exige que el usuario haya
iniciado chat con el bot que envía), así que el aviso lo envía ``telegram_boarding_bot``.
Nunca lanza excepciones: devuelve el motivo para registrarlo y reintentar después.
"""
from __future__ import annotations

import logging
from typing import Callable

import httpx

logger = logging.getLogger(__name__)

SENT = "sent"


class PassengerNotifier:
    def __init__(
        self,
        base_url: str,
        token_lookup: Callable[[], str | None],
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base = (base_url or "").rstrip("/")
        self._token_lookup = token_lookup
        self._timeout = timeout_seconds
        self._transport = transport

    @property
    def enabled(self) -> bool:
        return bool(self._base)

    def notify_boarding(self, ticket_id: int, status: str) -> str:
        """Devuelve ``"sent"`` o el motivo del fallo (``disabled``, ``no_token``, ``unreachable``,
        ``http_<código>``, ``not_linked``, ``status_mismatch``, ``send_failed``, ``ticket_not_found``)."""
        if not self.enabled:
            return "disabled"
        try:
            token = self._token_lookup()
        except Exception:  # noqa: BLE001 — sin token no se intenta el envío
            logger.warning("No se pudo leer el token interno del bot de pasajeros")
            return "no_token"
        if not token:
            return "no_token"
        try:
            with httpx.Client(transport=self._transport, timeout=self._timeout) as client:
                response = client.post(
                    f"{self._base}/internal/notify-boarding",
                    json={"ticket_id": int(ticket_id), "status": status},
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            logger.warning("Bot de pasajeros no disponible (ticket %s): %s", ticket_id, type(exc).__name__)
            return "unreachable"
        if response.status_code != 200:
            return f"http_{response.status_code}"
        try:
            data = response.json()
        except ValueError:
            return "http_invalid_json"
        if data.get("sent") is True:
            return SENT
        return str(data.get("reason") or "not_sent")
