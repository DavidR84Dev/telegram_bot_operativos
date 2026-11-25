"""Endpoints de la Mini App de la Prueba de Ruta.

Seguridad igual que el preoperacional y el AST: las rutas ``/api/prueba-ruta/*`` exigen
``X-Telegram-Init-Data`` con firma HMAC-SHA256 válida del bot; la identidad sale de ahí.

* ``GET  /prueba-ruta``              página HTML de la Mini App (pública; no tiene datos)
* ``GET  /api/prueba-ruta/form``     procedimiento, preguntas y fotos
* ``GET  /api/prueba-ruta/context``  empleado, autorización por contrato y vehículo reservado
* ``POST /api/prueba-ruta``          recibe el formulario -> 202 + ``pendiente``
  (403 "Contrato No Autorizado"; 409 sin preoperacional; 410 reenvío sin señal vencido)
* ``GET  /api/prueba-ruta/{uuid}``   estado de un envío propio
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
from ..preop.models import PruebaRuta
from ..preop.router import _LOGO_URL, _identity, _logo_version, firma_previa_diferida, tiempos_diferido
from . import service
from .catalogo import form_definition
from .schemas import PruebaRutaIn, PruebaRutaOut

logger = logging.getLogger(__name__)
router = APIRouter()
_HTML = Path(__file__).resolve().parent.parent / "static" / "prueba_ruta.html"


@dataclass
class PruebaRutaServices:
    """Dependencias del módulo (se cuelgan de ``app.state.prueba_ruta`` en el arranque)."""

    db: Database
    bot_token: str
    max_age_seconds: int
    autorizados: frozenset[str]  # acuerdos autorizados, ya normalizados (acuerdos.normalizar_lista)
    wake_worker: Any = None


def _services(request: Request) -> PruebaRutaServices:
    svc = getattr(request.app.state, "prueba_ruta", None)
    if svc is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Prueba de Ruta no disponible")
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


@router.get("/prueba-ruta", response_class=HTMLResponse, include_in_schema=False)
def prueba_ruta_page() -> HTMLResponse:
    html = _HTML.read_text(encoding="utf-8").replace(f'"{_LOGO_URL}"', f'"{_LOGO_URL}?v={_logo_version()}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@router.get("/api/prueba-ruta/form")
def prueba_ruta_form(_identity: Identity) -> dict[str, Any]:
    return form_definition()


@router.get("/api/prueba-ruta/context")
async def prueba_ruta_context(request: Request, identity: Identity) -> dict[str, Any]:
    svc = _services(request)
    empleado = await asyncio.to_thread(preop_service.employee_for_telegram, svc.db, identity.user_id)
    if empleado is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Tu ID de Telegram ({identity.user_id}) no está registrado como PIN de un empleado activo",
        )
    return await asyncio.to_thread(service.form_context, svc.db, empleado, identity.user_id, svc.autorizados)


def _out(prueba: PruebaRuta, duplicated: bool = False) -> PruebaRutaOut:
    return PruebaRutaOut(id=prueba.id, uuid=prueba.uuid, sync_status=prueba.sync_status.value,
                         odoo_id=prueba.odoo_id, odoo_name=prueba.odoo_name, no_conformes=prueba.no_conformes,
                         duplicado=duplicated)


@router.post("/api/prueba-ruta", status_code=status.HTTP_202_ACCEPTED, response_model=PruebaRutaOut,
             dependencies=[Depends(_firma_previa)])
async def prueba_ruta_submit(
    body: PruebaRutaIn,
    request: Request,
    x_telegram_init_data: Annotated[str | None, Header()] = None,
) -> PruebaRutaOut:
    """Guarda y responde de inmediato (202). El envío a Odoo lo hace el worker en segundo plano."""
    svc = _services(request)
    try:
        if body.diferido is None:
            identity = _identity(x_telegram_init_data, svc.bot_token, svc.max_age_seconds)
            prueba, duplicated = await asyncio.to_thread(service.submit, svc.db, identity, body, svc.autorizados)
        else:
            identity, tiempos = tiempos_diferido(body.diferido, x_telegram_init_data, svc.bot_token,
                                                 svc.max_age_seconds)
            prueba, duplicated = await asyncio.to_thread(
                service.submit_diferido, svc.db, identity, body, tiempos, svc.autorizados)
    except preop_service.EnvioVencidoError as exc:
        raise HTTPException(status.HTTP_410_GONE, "Envío vencido: superó 1 hora sin señal") from exc
    except preop_service.SubmissionError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    if not duplicated and svc.wake_worker is not None:
        svc.wake_worker()
    logger.info("Prueba de ruta recibida id=%s uuid=%s usuario=%s duplicado=%s novedades=%s diferido=%s",
                prueba.id, prueba.uuid, identity.user_id, duplicated, prueba.no_conformes, body.diferido is not None)
    return _out(prueba, duplicated)


@router.get("/api/prueba-ruta/{prueba_uuid}", response_model=PruebaRutaOut)
async def prueba_ruta_status(prueba_uuid: str, request: Request, identity: Identity) -> PruebaRutaOut:
    svc = _services(request)

    def _load() -> PruebaRuta | None:
        with svc.db.session() as session:
            return session.scalar(select(PruebaRuta).where(
                PruebaRuta.uuid == prueba_uuid.lower(),
                PruebaRuta.telegram_user_id == identity.user_id,
            ))

    prueba = await asyncio.to_thread(_load)
    if prueba is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    return _out(prueba)
