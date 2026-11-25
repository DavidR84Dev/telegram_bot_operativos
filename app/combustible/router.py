"""Endpoints de la Mini App de combustible.

Seguridad igual que los demás formularios: las rutas ``/api/combustible/*`` exigen
``X-Telegram-Init-Data`` con firma HMAC-SHA256 válida del bot; la identidad sale de ahí.

* ``GET  /combustible``              página HTML de la Mini App (pública; no tiene datos)
* ``GET  /api/combustible/form``     tipos de combustible, pestañas y proveedores aprobados
* ``GET  /api/combustible/context``  comprador, vehículo reservado, contrato y último kilometraje
* ``POST /api/combustible``          recibe el formulario -> 202 + ``pendiente``
  (409 sin preoperacional; 422 validación o kilometraje menor; 410 reenvío sin señal vencido)
* ``GET  /api/combustible/{uuid}``   estado de un envío propio
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
from ..preop.models import Combustible
from ..preop.router import _LOGO_URL, _identity, _logo_version, firma_previa_diferida, tiempos_diferido
from . import service
from .schemas import CombustibleIn, CombustibleOut

logger = logging.getLogger(__name__)
router = APIRouter()
_HTML = Path(__file__).resolve().parent.parent / "static" / "combustible.html"


@dataclass
class CombustibleServices:
    """Dependencias del módulo (se cuelgan de ``app.state.combustible`` en el arranque)."""

    db: Database
    bot_token: str
    max_age_seconds: int
    wake_worker: Any = None
    odometro: Any = None  # OdooWriter.ultimo_odometro (último odómetro en vivo)


def _services(request: Request) -> CombustibleServices:
    svc = getattr(request.app.state, "combustible", None)
    if svc is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Combustible no disponible")
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


@router.get("/combustible", response_class=HTMLResponse, include_in_schema=False)
def combustible_page() -> HTMLResponse:
    html = _HTML.read_text(encoding="utf-8").replace(f'"{_LOGO_URL}"', f'"{_LOGO_URL}?v={_logo_version()}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@router.get("/api/combustible/form")
async def combustible_form(request: Request, _identity: Identity) -> dict[str, Any]:
    return await asyncio.to_thread(service.form_full, _services(request).db)


@router.get("/api/combustible/context")
async def combustible_context(request: Request, identity: Identity) -> dict[str, Any]:
    svc = _services(request)
    empleado = await asyncio.to_thread(preop_service.employee_for_telegram, svc.db, identity.user_id)
    if empleado is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Tu ID de Telegram ({identity.user_id}) no está registrado como PIN de un empleado activo",
        )
    return await asyncio.to_thread(service.form_context, svc.db, empleado, identity.user_id, svc.odometro)


def _out(registro: Combustible, duplicated: bool = False) -> CombustibleOut:
    return CombustibleOut(id=registro.id, uuid=registro.uuid, sync_status=registro.sync_status.value,
                          odoo_id=registro.odoo_id, odoo_name=registro.odoo_name, total=float(registro.total),
                          duplicado=duplicated)


@router.post("/api/combustible", status_code=status.HTTP_202_ACCEPTED, response_model=CombustibleOut,
             dependencies=[Depends(_firma_previa)])
async def combustible_submit(
    body: CombustibleIn,
    request: Request,
    x_telegram_init_data: Annotated[str | None, Header()] = None,
) -> CombustibleOut:
    """Guarda y responde de inmediato (202). El envío a Odoo lo hace el worker en segundo plano."""
    svc = _services(request)
    try:
        if body.diferido is None:
            identity = _identity(x_telegram_init_data, svc.bot_token, svc.max_age_seconds)
            registro, duplicated = await asyncio.to_thread(service.submit, svc.db, identity, body, svc.odometro)
        else:
            identity, tiempos = tiempos_diferido(body.diferido, x_telegram_init_data, svc.bot_token,
                                                 svc.max_age_seconds)
            registro, duplicated = await asyncio.to_thread(
                service.submit_diferido, svc.db, identity, body, tiempos, svc.odometro)
    except preop_service.EnvioVencidoError as exc:
        raise HTTPException(status.HTTP_410_GONE, "Envío vencido: superó 1 hora sin señal") from exc
    except preop_service.SubmissionError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    if not duplicated and svc.wake_worker is not None:
        svc.wake_worker()
    logger.info("Combustible recibido id=%s uuid=%s usuario=%s duplicado=%s gastos=%s diferido=%s",
                registro.id, registro.uuid, identity.user_id, duplicated, registro.para_gastos,
                body.diferido is not None)
    return _out(registro, duplicated)


@router.get("/api/combustible/{combustible_uuid}", response_model=CombustibleOut)
async def combustible_status(combustible_uuid: str, request: Request, identity: Identity) -> CombustibleOut:
    svc = _services(request)

    def _load() -> Combustible | None:
        with svc.db.session() as session:
            return session.scalar(select(Combustible).where(
                Combustible.uuid == combustible_uuid.lower(),
                Combustible.telegram_user_id == identity.user_id,
            ))

    registro = await asyncio.to_thread(_load)
    if registro is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    return _out(registro)
