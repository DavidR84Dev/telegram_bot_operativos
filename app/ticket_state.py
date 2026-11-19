"""Reglas de estado de un ticket. Replican las de la página de Odoo.

Referencia: ``programacion_operativos/controllers/main.py`` (``form_accept``)
y ``reservations_tickets.action_board``. Este módulo NO escribe nada.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

# Orden de progreso: solo se notifica cuando el estado AVANZA.
STATUS_RANK = {"enviado": 0, "abordado1": 1, "abordado2": 2}

STATUS_LABELS = {
    "solicitud": "En Solicitud",
    "enviado": "Enviado (válido)",
    "abordado1": "Abordado 1ra Ruta",
    "abordado2": "Abordado 2da Ruta",
    "cancelado": "Cancelado",
}

# Solo el path: el host del QR se ignora (opción B), porque el QR de qa2
# puede apuntar a LAN y Telegram exige HTTPS público.
_TOKEN_RE = re.compile(r"/form/accept/([A-Za-z0-9_\-]{20,128})(?![A-Za-z0-9_\-])")


def extract_token(payload: str) -> str | None:
    """Extrae el access_token de una URL ``.../form/accept/<token>``."""
    match = _TOKEN_RE.search((payload or "").strip())
    return match.group(1) if match else None


def is_expired(ticket: dict[str, Any], now: datetime) -> bool:
    raw = ticket.get("token_expiration")
    if not raw:
        return False
    expires = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    return now > expires


def has_pending_route_2(ticket: dict[str, Any]) -> bool:
    return bool(
        ticket.get("route_origin_2") and ticket.get("route_destination_2")
        and not ticket.get("route_origin_bol_2")
    )


def is_boardable(ticket: dict[str, Any], now: datetime) -> bool:
    """La página muestra «Confirmar» solo en estos casos, y con token vigente."""
    if is_expired(ticket, now):
        return False
    status = ticket.get("status")
    return status == "enviado" or (status == "abordado1" and has_pending_route_2(ticket))


def is_final(ticket: dict[str, Any]) -> bool:
    """Sin más lecturas posibles: se deja de vigilar."""
    status = ticket.get("status")
    if status in ("abordado2", "cancelado"):
        return True
    return status == "abordado1" and not has_pending_route_2(ticket)


def rank(status: str | None) -> int:
    return STATUS_RANK.get(status or "", -1)


def label(status: str | None) -> str:
    return STATUS_LABELS.get(status or "", status or "—")


def describe(ticket: dict[str, Any], now: datetime) -> str:
    """Motivo en texto plano por el que no se puede confirmar (vacío si sí se puede)."""
    if is_boardable(ticket, now):
        return ""
    if is_expired(ticket, now):
        return "El token de este ticket está vencido. Solicita uno nuevo a la empresa."
    status = ticket.get("status")
    if status == "cancelado":
        return "Este ticket está cancelado."
    if status == "abordado2" or (status == "abordado1" and not has_pending_route_2(ticket)):
        return "Este ticket ya fue abordado; no requiere más lecturas."
    if status == "solicitud":
        return "Este ticket está En Solicitud: aún no tiene vehículo asignado."
    return "Este ticket no admite confirmación en este momento."
