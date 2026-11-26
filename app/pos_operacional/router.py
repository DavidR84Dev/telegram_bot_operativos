"""Endpoints de la Mini App del Pos-Operacional.

Seguridad igual que los demás formularios: las rutas ``/api/pos-operacional/*`` exigen
``X-Telegram-Init-Data`` con firma HMAC-SHA256 válida del bot; la identidad sale de ahí.

* ``GET  /pos-operacional``              página HTML de la Mini App (pública; no tiene datos)
* ``GET  /api/pos-operacional/form``     límites del formulario
* ``GET  /api/pos-operacional/context``  conductor, contrato, vehículo reservado, KM inicial y horas por defecto
  (403 sin PIN)
* ``POST /api/pos-operacional``          recibe el formulario -> 202 + ``pendiente``
  (403 sin empleado; 409 uuid ajeno; 422 validación; 410 reenvío sin señal vencido)
* ``GET  /api/pos-operacional/{uuid}``   estado de un envío propio
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

from ..preop import service as preop_service
from ..preop.db import Database
from ..preop.models import PosOperacional
from ..preop.router import (
    _LOGO_URL,
    _identity,
    _logo_version,
    contexto_http,
    firma_previa_diferida,
    http_error,
    tiempos_diferido,
)
from . import service
from .catalogo import form_definition
from .schemas import PosOperacionalIn, PosOperacionalOut

logger = logging.getLogger(__name__)
router = APIRouter()
_HTML = Path(__file__).resolve().parent.parent / "static" / "pos_operacional.html"


@dataclass
class PosOperacionalServices:
    """Dependencias del módulo (se cuelgan de ``app.state.pos_operacional`` en el arranque)."""

    db: Database
    bot_token: str
    max_age_seconds: int
    wake_worker: Any = None
    # async (recibido): avisa en el chat que el envío se recibió (también el reenvío sin señal aceptado).
    on_accepted: Any = None


def _services(request: Request) -> PosOperacionalServices:
    svc = getattr(request.app.state, "pos_operacional", None)
    if svc is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Pos-Operacional no disponible")
    return svc


def telegram_identity(
    request: Request,
    x_telegram_init_data: Annotated[str | None, Header()] = None,
) -> preop_service.TelegramIdentity:
    svc = _services(request)
    return _identity(x_telegram_init_data, svc.bot_token, svc.max_age_seconds)


Identity = Annotated[preop_service.TelegramIdentity, Depends(telegram_identity)]


def _firma_previa(request: Request, x_telegram_init_data: Annotated[str | None, Header()] = None) -> None:
    svc = _services(request)
    firma_previa_diferida(x_telegram_init_data, svc.bot_token, svc.max_age_seconds)


@router.get("/pos-operacional", response_class=HTMLResponse, include_in_schema=False)
def pos_operacional_page() -> HTMLResponse:
    html = _HTML.read_text(encoding="utf-8").replace(f'"{_LOGO_URL}"', f'"{_LOGO_URL}?v={_logo_version()}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@router.get("/api/pos-operacional/form")
def pos_operacional_form(_identity: Identity) -> dict[str, Any]:
    return form_definition()


@router.get("/api/pos-operacional/context")
async def pos_operacional_context(request: Request, identity: Identity) -> dict[str, Any]:
    svc = _services(request)
    empleado = await asyncio.to_thread(preop_service.employee_for_telegram, svc.db, identity.user_id)
    if empleado is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Tu ID de Telegram ({identity.user_id}) no está registrado como PIN de un empleado activo",
        )
    return await contexto_http(service.form_context, svc.db, empleado, identity.user_id)


def _out(recibido: service.PosRecibido, duplicated: bool = False) -> PosOperacionalOut:
    return PosOperacionalOut(id=recibido.id, uuid=recibido.uuid, sync_status=recibido.sync_status.value,
                             odoo_id=recibido.odoo_id, duplicado=duplicated)


@router.post("/api/pos-operacional", status_code=status.HTTP_202_ACCEPTED, response_model=PosOperacionalOut,
             dependencies=[Depends(_firma_previa)])
async def pos_operacional_submit(
    body: PosOperacionalIn,
    request: Request,
    x_telegram_init_data: Annotated[str | None, Header()] = None,
) -> PosOperacionalOut:
    """Guarda y responde de inmediato (202). El envío a Odoo lo hace el worker en segundo plano."""
    svc = _services(request)
    try:
        if body.diferido is None:
            identity = _identity(x_telegram_init_data, svc.bot_token, svc.max_age_seconds)
            recibido, duplicated = await asyncio.to_thread(service.submit, svc.db, identity, body)
        else:
            identity, tiempos = tiempos_diferido(body.diferido, x_telegram_init_data, svc.bot_token,
                                                 svc.max_age_seconds)
            recibido, duplicated = await asyncio.to_thread(service.submit_diferido, svc.db, identity, body, tiempos)
    except preop_service.EnvioVencidoError as exc:
        raise HTTPException(status.HTTP_410_GONE, "Envío vencido: superó 1 hora sin señal") from exc
    except preop_service.SubmissionError as exc:
        raise http_error(exc) from exc
    if not duplicated and svc.wake_worker is not None:
        svc.wake_worker()
    if not duplicated and svc.on_accepted is not None:
        try:
            await svc.on_accepted(recibido)
        except Exception:  # ya está guardado: el aviso fallido no debe devolver error al conductor
            logger.exception("No se pudo avisar la recepción del pos-operacional %s", recibido.id)
    logger.info("Pos-operacional recibido id=%s uuid=%s usuario=%s duplicado=%s diferido=%s",
                recibido.id, recibido.uuid, identity.user_id, duplicated, body.diferido is not None)
    return _out(recibido, duplicated)


@router.get("/api/pos-operacional/{pos_uuid}", response_model=PosOperacionalOut)
async def pos_operacional_status(pos_uuid: str, request: Request, identity: Identity) -> PosOperacionalOut:
    svc = _services(request)

    def _load() -> service.PosRecibido | None:
        with svc.db.session() as session:
            pos = session.scalar(select(PosOperacional).where(
                PosOperacional.uuid == pos_uuid.lower(), PosOperacional.telegram_user_id == identity.user_id))
            return service.resumen_recibido(pos) if pos is not None else None

    recibido = await asyncio.to_thread(_load)
    if recibido is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    return _out(recibido)
