"""Textos HTML para Telegram."""
from __future__ import annotations

from html import escape

from . import ticket_state


def _route(origin: str | None, destination: str | None) -> str:
    return f"{escape(origin or '—')} → {escape(destination or '—')}"


def ticket_card(ticket: dict) -> str:
    lines = [
        f"🎫 <b>{escape(ticket.get('ticket_number') or '—')}</b>",
        f"👤 Pasajero: {escape(ticket.get('passenger_name') or '—')}",
        f"🚌 Vehículo: {escape(ticket.get('vehicle') or '—')}",
        f"🗺 Ruta 1: {_route(ticket.get('route_origin'), ticket.get('route_destination'))}",
    ]
    if ticket.get("route_origin_2") and ticket.get("route_destination_2"):
        lines.append(
            f"🗺 Ruta 2: {_route(ticket.get('route_origin_2'), ticket.get('route_destination_2'))}"
        )
    lines.append(f"📌 Estado: <b>{escape(ticket_state.label(ticket.get('status')))}</b>")
    return "\n".join(lines)


def boarding_confirmed(ticket: dict) -> str:
    status = ticket.get("status")
    leg = "1ra Ruta" if status == "abordado1" else "2da Ruta"
    pending = ticket_state.has_pending_route_2(ticket) and status == "abordado1"
    tail = "\n⏳ Falta la confirmación de la 2da Ruta." if pending else ""
    return (
        f"✅ <b>Abordaje confirmado ({leg})</b>\n\n"
        f"{ticket_card(ticket)}{tail}"
    )
