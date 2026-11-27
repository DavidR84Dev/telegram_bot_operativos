"""Endpoints de la Mini App de Correcciones Post y Gastos.

Seguridad igual que los demás formularios: las rutas ``/api/correcciones/*`` exigen ``X-Telegram-Init-Data`` con
firma HMAC-SHA256 válida del bot; la identidad sale de ahí.

* ``GET  /correcciones``               página HTML de la Mini App (pública; no tiene datos)
* ``GET  /api/correcciones/form``      límites del formulario
* ``GET  /api/correcciones/context``   gastos rechazados, Post-operacionales pendientes y planillas no enviadas
  (jornadas sin Post) del conductor, en vivo desde Odoo (403 sin PIN; con Odoo caído responde 200 con ``odoo_ok`` falso
  y un mensaje)
* ``POST /api/correcciones``           recibe una corrección (``tipo`` = ``gasto`` | ``pos`` | ``planilla``) -> 202 + ``pendiente``
  (403 sin empleado o registro ajeno; 404/409 el registro no existe o ya no está pendiente; 422 validación;
  410 reenvío sin señal vencido; 503 Odoo no responde)
* ``GET  /api/correcciones/{uuid}``    estado de un envío propio
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
from ..preop.models import Correccion
from ..preop.odoo_writer import OdooWriter
from ..preop.router import (
    _LOGO_URL,
    _versionar_escanear_js,
    _identity,
    _logo_version,
    firma_previa_diferida,
    http_error,
    tiempos_diferido,
)
from . import service
from .catalogo import form_definition
from .schemas import CorreccionIn, CorreccionOut

logger = logging.getLogger(__name__)
router = APIRouter()
_HTML = Path(__file__).resolve().parent.parent / "static" / "correcciones.html"


@dataclass
class CorreccionesServices:
    """Dependencias del módulo (se cuelgan de ``app.state.correcciones`` en el arranque)."""

    db: Database
    writer: OdooWriter
    bot_token: str
    max_age_seconds: int
    wake_worker: Any = None
    # async (recibida): avisa en el chat que la corrección se recibió (también el reenvío sin señal aceptado).
    on_accepted: Any = None


def _services(request: Request) -> CorreccionesServices:
    svc = getattr(request.app.state, "correcciones", None)
    if svc is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Correcciones no disponible")
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


@router.get("/correcciones", response_class=HTMLResponse, include_in_schema=False)
def correcciones_page() -> HTMLResponse:
    html = _versionar_escanear_js(
        _HTML.read_text(encoding="utf-8").replace(f'"{_LOGO_URL}"', f'"{_LOGO_URL}?v={_logo_version()}"')
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@router.get("/api/correcciones/form")
def correcciones_form(_identity: Identity) -> dict[str, Any]:
    return form_definition()


@router.get("/api/correcciones/context")
async def correcciones_context(request: Request, identity: Identity) -> dict[str, Any]:
    svc = _services(request)
    empleado = await asyncio.to_thread(preop_service.employee_for_telegram, svc.db, identity.user_id)
    if empleado is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Tu ID de Telegram ({identity.user_id}) no está registrado como PIN de un empleado activo",
        )
    return await asyncio.to_thread(service.contexto, svc.db, svc.writer, empleado)


def _out(recibida: service.CorreccionRecibida, duplicated: bool = False) -> CorreccionOut:
    return CorreccionOut(id=recibida.id, uuid=recibida.uuid, tipo=recibida.tipo,
                         sync_status=recibida.sync_status.value, duplicado=duplicated)


@router.post("/api/correcciones", status_code=status.HTTP_202_ACCEPTED, response_model=CorreccionOut,
             dependencies=[Depends(_firma_previa)])
async def correcciones_submit(
    body: CorreccionIn,
    request: Request,
    x_telegram_init_data: Annotated[str | None, Header()] = None,
) -> CorreccionOut:
    """Guarda y responde de inmediato (202). El envío a Odoo lo hace el worker en segundo plano."""
    svc = _services(request)
    try:
        if body.diferido is None:
            identity = _identity(x_telegram_init_data, svc.bot_token, svc.max_age_seconds)
            recibida, duplicated = await asyncio.to_thread(service.submit, svc.db, svc.writer, identity, body)
        else:
            identity, tiempos = tiempos_diferido(body.diferido, x_telegram_init_data, svc.bot_token,
                                                 svc.max_age_seconds)
            recibida, duplicated = await asyncio.to_thread(
                service.submit_diferido, svc.db, svc.writer, identity, body, tiempos)
    except preop_service.EnvioVencidoError as exc:
        raise HTTPException(status.HTTP_410_GONE, "Envío vencido: superó 1 hora sin señal") from exc
    except preop_service.SubmissionError as exc:
        raise http_error(exc) from exc
    if not duplicated and svc.wake_worker is not None:
        svc.wake_worker()
    if not duplicated and svc.on_accepted is not None:
        try:
            await svc.on_accepted(recibida)
        except Exception:  # ya está guardada: el aviso fallido no debe devolver error al conductor
            logger.exception("No se pudo avisar la recepción de la corrección %s", recibida.id)
    logger.info("Corrección recibida id=%s uuid=%s tipo=%s usuario=%s duplicada=%s diferida=%s",
                recibida.id, recibida.uuid, recibida.tipo, identity.user_id, duplicated, body.diferido is not None)
    return _out(recibida, duplicated)


@router.get("/api/correcciones/{correccion_uuid}", response_model=CorreccionOut)
async def correcciones_status(correccion_uuid: str, request: Request, identity: Identity) -> CorreccionOut:
    svc = _services(request)

    def _load() -> service.CorreccionRecibida | None:
        with svc.db.session() as session:
            c = session.scalar(select(Correccion).where(
                Correccion.uuid == correccion_uuid.lower(), Correccion.telegram_user_id == identity.user_id))
            return service.resumen_recibido(c) if c is not None else None

    recibida = await asyncio.to_thread(_load)
    if recibida is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    return _out(recibida)
