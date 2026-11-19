"""Lectura de un QR ya decodificado: lo usan el chat y la Mini App del escáner.

Solo consulta Odoo (XML-RPC de lectura). Si el ticket es confirmable y se pasa
un chat de validador, registra la vigilancia para avisar cuando Odoo registre
el abordaje. El llamador debe arrancar el sondeo (``Watcher.ensure_running``)
desde el event loop.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import ticket_state
from .odoo_client import OdooClient, OdooClientError
from .odoo_queries import find_ticket_by_token
from .watch_store import WatchStore
from .watcher import new_expiry, utc_now

STATUS_MESSAGES = {
    "invalid_qr": "Este QR no corresponde a un ticket de abordaje.",
    "not_found": "No existe ningún ticket asociado a este QR.",
    "odoo_error": "No pude consultar Odoo en este momento. Intenta de nuevo.",
}


@dataclass
class ScanOutcome:
    status: str  # invalid_qr | not_found | odoo_error | ok
    token: str = ""
    ticket: dict[str, Any] | None = None
    boardable: bool = False
    watching: bool = False


def scan_qr(
    odoo: OdooClient,
    store: WatchStore,
    ttl_hours: int,
    qr_text: str,
    validator_chat_id: int | None,
    now: datetime | None = None,
) -> ScanOutcome:
    token = ticket_state.extract_token(qr_text)
    if not token:
        return ScanOutcome("invalid_qr")
    try:
        ticket = find_ticket_by_token(odoo, token)
    except OdooClientError:
        return ScanOutcome("odoo_error", token=token)
    if not ticket:
        return ScanOutcome("not_found", token=token)

    now = now or utc_now()
    boardable = ticket_state.is_boardable(ticket, now)
    watching = False
    if boardable and validator_chat_id is not None:
        store.add(ticket["id"], validator_chat_id, ticket["ticket_number"], ticket["status"],
                  now, new_expiry(now, ttl_hours))
        watching = True
    return ScanOutcome("ok", token=token, ticket=ticket, boardable=boardable, watching=watching)
