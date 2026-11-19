"""Consultas de solo lectura a Odoo (hr.employee y reservations.tickets)."""
from __future__ import annotations

from typing import Any

from .odoo_client import OdooClient

EMPLOYEE_MODEL = "hr.employee"
TICKET_MODEL = "reservations.tickets"
BOT_CONFIG_MODEL = "bot.telegram.config"

TICKET_FIELDS = [
    "id",
    "ticket_number",
    "passenger_name",
    "status",
    "vehicle_id",
    "route_origin",
    "route_destination",
    "route_origin_2",
    "route_destination_2",
    "route_origin_bol",
    "route_origin_bol_2",
    "access_token",
    "token_expiration",
    "id_telegram",
]


def _m2o_name(value: Any) -> str:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return value[1] or ""
    return value or ""


def normalize_ticket(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "ticket_number": row.get("ticket_number") or "",
        "passenger_name": (row.get("passenger_name") or "").upper(),
        "status": row.get("status") or "",
        "vehicle": _m2o_name(row.get("vehicle_id")),
        "route_origin": _m2o_name(row.get("route_origin")),
        "route_destination": _m2o_name(row.get("route_destination")),
        "route_origin_2": _m2o_name(row.get("route_origin_2")),
        "route_destination_2": _m2o_name(row.get("route_destination_2")),
        "route_origin_bol": bool(row.get("route_origin_bol")),
        "route_origin_bol_2": bool(row.get("route_origin_bol_2")),
        "access_token": row.get("access_token") or "",
        "token_expiration": row.get("token_expiration") or "",
        "id_telegram": row.get("id_telegram") or "",
    }


def find_employee_by_pin(client: OdooClient, telegram_user_id: str) -> dict[str, Any] | None:
    """Empleado activo cuyo campo ``pin`` coincide con el id de Telegram."""
    rows = client.search_read(
        EMPLOYEE_MODEL,
        [("pin", "=", telegram_user_id), ("active", "=", True)],
        ["id", "name"],
        limit=1,
    )
    return rows[0] if rows else None


def find_ticket_by_token(client: OdooClient, token: str) -> dict[str, Any] | None:
    rows = client.search_read(TICKET_MODEL, [("access_token", "=", token)], TICKET_FIELDS, limit=1)
    return normalize_ticket(rows[0]) if rows else None


def find_tickets_by_ids(client: OdooClient, ticket_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not ticket_ids:
        return {}
    rows = client.search_read(TICKET_MODEL, [("id", "in", ticket_ids)], TICKET_FIELDS, limit=len(ticket_ids))
    return {row["id"]: normalize_ticket(row) for row in rows}


def find_internal_token(client: OdooClient) -> str | None:
    """Token interno compartido con el bot de pasajeros (registro activo de bot.telegram.config)."""
    rows = client.search_read(
        BOT_CONFIG_MODEL,
        [("active", "=", True), ("flag", "=", True)],
        ["internal_token"],
        limit=1,
    )
    return (rows[0].get("internal_token") or None) if rows else None
