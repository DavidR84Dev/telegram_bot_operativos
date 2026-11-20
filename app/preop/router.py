"""Endpoints de la Mini App del preoperacional.

Seguridad: todas las rutas ``/api/preop/*`` exigen la cabecera
``X-Telegram-Init-Data`` (``Telegram.WebApp.initData``) con firma HMAC-SHA256
válida del bot y antigüedad acotada. La identidad sale de ahí, nunca del cuerpo.

* ``GET  /preoperacional``        página HTML de la Mini App (pública; no tiene datos)
* ``GET  /api/preop/form``        catálogo de ítems (secciones, opciones, medidas)
* ``GET  /api/preop/context``     empleado + vehículos + extractos vigentes (autocompletado)
* ``POST /api/preop``             recibe el formulario -> 202 + estado ``pendiente``
* ``GET  /api/preop/{uuid}``      estado de sincronización de un envío propio
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from ..webapp_auth import InitDataError, validate_init_data_user
from . import service
from .checklist import form_definition
from .db import Database
from .models import Preoperacional
from .schemas import PreoperacionalIn, PreoperacionalOut

logger = logging.getLogger(__name__)
router = APIRouter()
_HTML = Path(__file__).resolve().parent.parent / "static" / "preoperacional.html"


@dataclass
class PreopServices:
    """Dependencias del módulo (se cuelgan de ``app.state.preop`` en el arranque)."""

    db: Database
    bot_token: str
    max_age_seconds: int
    wake_worker: Any = None  # callable sin argumentos: despierta el worker tras un envío


def _services(request: Request) -> PreopServices:
    svc = getattr(request.app.state, "preop", None)
    if svc is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Preoperacional no disponible")
    return svc


def telegram_identity(
    request: Request,
    x_telegram_init_data: Annotated[str | None, Header()] = None,
) -> service.TelegramIdentity:
    """Dependencia de seguridad: valida initData (HMAC-SHA256 con el token del bot)."""
    svc = _services(request)
    try:
        user = validate_init_data_user(x_telegram_init_data or "", svc.bot_token, svc.max_age_seconds)
    except InitDataError as exc:
        logger.warning("Preop: initData rechazado (%s); longitud=%d", exc, len(x_telegram_init_data or ""))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sesión de Telegram no válida") from exc
    full_name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p).strip()
    username = user.get("username")
    return service.TelegramIdentity(
        user_id=int(user["id"]),
        username=str(username)[:64] if username else None,
        full_name=full_name or str(user["id"]),
    )


Identity = Annotated[service.TelegramIdentity, Depends(telegram_identity)]


@router.get("/preoperacional", response_class=HTMLResponse, include_in_schema=False)
def preop_page() -> str:
    return _HTML.read_text(encoding="utf-8")


@router.get("/api/preop/form")
def preop_form(_identity: Identity) -> dict[str, Any]:
    return form_definition()


@router.get("/api/preop/context")
async def preop_context(request: Request, identity: Identity) -> dict[str, Any]:
    svc = _services(request)
    empleado = await asyncio.to_thread(service.employee_for_telegram, svc.db, identity.user_id)
    if empleado is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Tu ID de Telegram ({identity.user_id}) no está registrado como PIN de un empleado activo",
        )
    return await asyncio.to_thread(service.form_context, svc.db, empleado)


def _out(preop: Preoperacional, duplicated: bool = False) -> PreoperacionalOut:
    return PreoperacionalOut(
        id=preop.id, uuid=preop.uuid, sync_status=preop.sync_status.value, odoo_id=preop.odoo_id,
        odoo_name=preop.odoo_name, no_conformes=preop.no_conformes, duplicado=duplicated,
    )


@router.post("/api/preop", status_code=status.HTTP_202_ACCEPTED, response_model=PreoperacionalOut)
async def preop_submit(body: PreoperacionalIn, request: Request, identity: Identity) -> PreoperacionalOut:
    """Guarda y responde de inmediato (202). El envío a Odoo lo hace el worker en segundo plano."""
    svc = _services(request)
    try:
        preop, duplicated = await asyncio.to_thread(service.submit, svc.db, identity, body)
    except service.SubmissionError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    if not duplicated and svc.wake_worker is not None:
        svc.wake_worker()
    logger.info("Preoperacional recibido id=%s uuid=%s usuario=%s duplicado=%s",
                preop.id, preop.uuid, identity.user_id, duplicated)
    return _out(preop, duplicated)


@router.get("/api/preop/{preop_uuid}", response_model=PreoperacionalOut)
async def preop_status(preop_uuid: str, request: Request, identity: Identity) -> PreoperacionalOut:
    svc = _services(request)

    def _load() -> Preoperacional | None:
        with svc.db.session() as session:
            return session.scalar(select(Preoperacional).where(
                Preoperacional.uuid == preop_uuid.lower(),
                Preoperacional.telegram_user_id == identity.user_id,  # solo envíos propios
            ))

    preop = await asyncio.to_thread(_load)
    if preop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    return _out(preop)
