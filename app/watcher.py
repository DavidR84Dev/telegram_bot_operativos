"""Sondeo de tickets vigilados (cada N s, solo mientras haya tickets pendientes).

Solo lee de Odoo (``fetch``) y avisa al validador (``notify``). La decisión
por vigilancia está en ``decide`` (pura, sin E/S), para poder probarla.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from . import ticket_state
from .passenger_notifier import SENT
from .watch_store import Watch, WatchStore

logger = logging.getLogger(__name__)

FetchFn = Callable[[list[int]], dict[int, dict]]
NotifyFn = Callable[[int, dict], Awaitable[None]]
# (ticket_id, estado) -> "sent" o motivo del fallo (ver passenger_notifier)
PassengerFn = Callable[[int, str], Awaitable[str]]


def utc_now() -> datetime:
    """Hora UTC naive: misma base que los campos Datetime de Odoo."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def decide(watch: Watch, ticket: dict | None, now: datetime) -> str:
    """Acción para una vigilancia: ``remove``, ``notify`` o ``keep``.

    * ticket inexistente (archivado/borrado) → remove
    * cancelado, token vencido o vigilancia caducada → remove
    * estado que avanza respecto al último notificado → notify
    * estado final tras (o sin) notificar → remove
    """
    if ticket is None:
        return "remove"
    if ticket.get("status") == "cancelado":
        return "remove"
    if ticket_state.is_expired(ticket, now) or now > watch.expires_at:
        return "remove"
    notify = ticket_state.rank(ticket.get("status")) > ticket_state.rank(watch.last_status)
    if notify:
        return "notify"
    if ticket_state.is_final(ticket):
        return "remove"
    return "keep"


class Watcher:
    def __init__(
        self,
        store: WatchStore,
        fetch: FetchFn,
        notify: NotifyFn,
        interval_seconds: int,
        clock: Callable[[], datetime] = utc_now,
        notify_passenger: PassengerFn | None = None,
    ) -> None:
        self._store = store
        self._fetch = fetch
        self._notify = notify
        self._interval = interval_seconds
        self._clock = clock
        self._notify_passenger = notify_passenger
        self._task: asyncio.Task | None = None
        # Fallos del aviso al pasajero ya registrados (evita repetir el log cada sondeo).
        self._passenger_logged: set[tuple[int, str, str]] = set()

    def ensure_running(self) -> None:
        """Arranca el bucle si hay vigilancias y no está ya corriendo."""
        if self._store.count() == 0:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="qr-validator-watcher")

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001 — el bucle no debe morir por un fallo puntual
                logger.exception("Fallo en el sondeo de tickets")
            if self._store.count() == 0:
                logger.info("Sin tickets pendientes: el sondeo se detiene")
                return

    async def poll_once(self) -> None:
        watches = self._store.all()
        if not watches:
            return
        now = self._clock()
        ids = sorted({w.ticket_id for w in watches})
        tickets = await asyncio.to_thread(self._fetch, ids)

        # 1) Aviso al pasajero: antes que a los validadores, para que una vigilancia
        #    solo se elimine cuando el pasajero ya fue avisado (o no puede serlo).
        if self._notify_passenger is not None:
            for ticket_id in ids:
                await self._notify_passenger_if_due(tickets.get(ticket_id))

        # 2) Aviso al validador y limpieza de vigilancias.
        for watch in watches:
            ticket = tickets.get(watch.ticket_id)
            action = decide(watch, ticket, now)
            if action == "notify":
                assert ticket is not None  # decide() solo devuelve "notify" con un ticket presente
                try:
                    await self._notify(watch.validator_chat_id, ticket)
                except Exception:  # noqa: BLE001 — se reintenta en el próximo sondeo
                    logger.warning("No se pudo notificar el ticket %s", watch.ticket_number)
                    continue
                self._store.update_status(watch.ticket_id, watch.validator_chat_id, ticket["status"])
                if ticket_state.is_final(ticket) and not self._passenger_pending(ticket):
                    self._store.remove(watch.ticket_id, watch.validator_chat_id)
            elif action == "remove":
                if self._passenger_pending(ticket):
                    assert ticket is not None  # _passenger_pending() ya descartó el caso None
                    if now <= watch.expires_at:
                        continue  # se conserva para reintentar el aviso al pasajero
                    logger.error(
                        "Vigilancia caducada sin aviso al pasajero: ticket %s, estado %s",
                        watch.ticket_number, ticket.get("status"),
                    )
                self._store.remove(watch.ticket_id, watch.validator_chat_id)

    def _passenger_pending(self, ticket: dict | None) -> bool:
        """Hay un abordaje (1.ª o 2.ª ruta) que aún no se avisó al pasajero."""
        if ticket is None or self._notify_passenger is None:
            return False
        status = ticket.get("status")
        if ticket_state.rank(status) < ticket_state.rank("abordado1"):
            return False
        return ticket_state.rank(status) > ticket_state.rank(self._store.passenger_status(ticket["id"]))

    async def _notify_passenger_if_due(self, ticket: dict | None) -> None:
        if not self._passenger_pending(ticket):
            return
        assert ticket is not None  # _passenger_pending() ya descartó el caso None
        assert self._notify_passenger is not None  # idem: _passenger_pending() también lo exige
        ticket_id, number, status = ticket["id"], ticket.get("ticket_number"), ticket["status"]
        if not ticket.get("id_telegram"):
            logger.warning("Ticket %s sin ID de Telegram: no se avisa al pasajero (estado %s)", number, status)
            self._store.set_passenger_status(ticket_id, status)
            return
        try:
            reason = await self._notify_passenger(ticket_id, status)
        except Exception:  # noqa: BLE001 — se reintenta en el próximo sondeo
            logger.exception("Fallo inesperado al avisar al pasajero del ticket %s", number)
            reason = "error"
        if reason == SENT:
            self._store.set_passenger_status(ticket_id, status)
            logger.info("Pasajero avisado del abordaje: ticket %s, estado %s", number, status)
            return
        key = (ticket_id, status, reason)
        if key not in self._passenger_logged:
            self._passenger_logged.add(key)
            logger.warning(
                "Aviso al pasajero pendiente: ticket %s, estado %s, motivo=%s (se reintenta cada sondeo)",
                number, status, reason,
            )

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()


def new_expiry(now: datetime, ttl_hours: int) -> datetime:
    return now + timedelta(hours=ttl_hours)
