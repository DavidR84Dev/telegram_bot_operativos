"""Mini App del escáner: página /scanner y API /api/scan (solo personal autorizado).

La identidad sale de ``initData`` firmado por Telegram (cabecera
``X-Telegram-Init-Data``), nunca del cuerpo de la petición.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import ticket_state
from .scan_service import STATUS_MESSAGES, ScanOutcome, scan_qr
from .watcher import utc_now
from .webapp_auth import InitDataError, validate_init_data

router = APIRouter()
logger = logging.getLogger(__name__)
_SCANNER_HTML = Path(__file__).parent / "static" / "scanner.html"

_PUBLIC_TICKET_FIELDS = (
    "ticket_number", "passenger_name", "vehicle", "status", "status_label",
    "route_origin", "route_destination", "route_origin_2", "route_destination_2",
    "route_origin_bol", "route_origin_bol_2",
)


class ScanIn(BaseModel):
    qr_text: str = Field(min_length=1, max_length=2048)
    # Diagnóstico del cliente (sin datos sensibles); solo se registra si falla la sesión.
    diag: str = Field(default="", max_length=300)


@router.get("/scanner", response_class=HTMLResponse)
def scanner_page() -> str:
    return _SCANNER_HTML.read_text(encoding="utf-8")


def _payload(outcome: ScanOutcome, webapp_base_url: str) -> dict[str, Any]:
    data: dict[str, Any] = {
        "status": outcome.status,
        "boardable": outcome.boardable,
        "watching": outcome.watching,
        "ticket": None,
        "odoo_url": None,
        "reason": "",
    }
    if outcome.status != "ok":
        data["reason"] = STATUS_MESSAGES.get(outcome.status, "No fue posible leer el QR.")
        return data
    ticket = outcome.ticket or {}
    data["ticket"] = {key: ticket.get(key, "") for key in _PUBLIC_TICKET_FIELDS}
    data["ticket"]["status_label"] = ticket_state.label(ticket.get("status"))
    data["odoo_url"] = f"{webapp_base_url.rstrip('/')}/form/accept/{outcome.token}"
    data["reason"] = ticket_state.describe(ticket, utc_now())
    return data


@router.post("/api/scan")
async def api_scan(
    body: ScanIn,
    request: Request,
    x_telegram_init_data: str | None = Header(default=None),
) -> dict[str, Any]:
    svc = request.app.state.services
    try:
        auth = validate_init_data(
            x_telegram_init_data or "",
            svc.settings.telegram_bot_token.get_secret_value(),
            svc.settings.webapp_auth_max_age_seconds,
        )
    except InitDataError as exc:
        # Solo diagnóstico: longitud de la cabecera y motivo, nunca el valor de initData.
        logger.warning("initData rechazado (%s); longitud cabecera=%d; diag=%s",
                       exc, len(x_telegram_init_data or ""), body.diag)
        raise HTTPException(status_code=401, detail="Sesión de Telegram no válida") from exc

    user_id = auth["user_id"]
    if not await asyncio.to_thread(svc.access.is_authorized, user_id):
        raise HTTPException(status_code=403, detail="No autorizado")

    # En chat privado el id del chat es el id del usuario: ahí llegan los avisos.
    outcome = await asyncio.to_thread(
        scan_qr, svc.odoo, svc.store, svc.settings.watch_ttl_hours, body.qr_text, int(user_id)
    )
    if outcome.watching:
        svc.watcher.ensure_running()
    return _payload(outcome, svc.settings.webapp_base_url)
