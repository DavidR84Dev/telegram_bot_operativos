"""Auditoría del Panel Admin (tabla ``admin_auditoria``): quién, cuándo, acción, objetivo y detalle.

El detalle nunca lleva valores de secretos (ni valores de parámetros del ``.env``: solo la clave).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..preop.models import AdminAuditoria
from ..preop.service import TelegramIdentity


def registrar(session: Session, identity: TelegramIdentity, accion: str, objetivo: str = "",
              detalle: str | None = None) -> AdminAuditoria:
    """Agrega la fila a la sesión del que llama: la acción y su auditoría se confirman juntas."""
    fila = AdminAuditoria(telegram_user_id=identity.user_id, nombre=identity.full_name[:255], accion=accion[:64],
                          objetivo=objetivo[:255], detalle=detalle)
    session.add(fila)
    session.flush()
    return fila


def listar(session: Session, limite: int = 100) -> list[dict]:
    filas = session.scalars(select(AdminAuditoria).order_by(AdminAuditoria.id.desc()).limit(limite)).all()
    return [{"id": f.id, "fecha": f.created_at.isoformat(), "telegram_user_id": f.telegram_user_id,
             "nombre": f.nombre, "accion": f.accion, "objetivo": f.objetivo, "detalle": f.detalle} for f in filas]
