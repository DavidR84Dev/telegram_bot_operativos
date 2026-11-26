"""Endpoints de la Mini App de Gastos Operativos.

Seguridad igual que los demás formularios: las rutas ``/api/gastos-operativos/*`` exigen
``X-Telegram-Init-Data`` con firma HMAC-SHA256 válida del bot; la identidad sale de ahí.

* ``GET  /gastos-operativos``              página HTML de la Mini App (pública; no tiene datos)
* ``GET  /api/gastos-operativos/form``     tipos de gasto y límites
* ``GET  /api/gastos-operativos/context``  conductor, vehículo reservado (o ``null``) y fecha de hoy
* ``POST /api/gastos-operativos``          recibe el formulario -> 202 + ``pendiente`` (retenido hasta la ubicación)
  (403 sin empleado; 422 validación; 410 reenvío sin señal vencido)
* ``GET  /api/gastos-operativos/{uuid}``   estado de un envío propio
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from ..preop import service as preop_service
from ..preop.db import Database
from ..preop.models import GastoOperativo, GastoOperativoLinea
from ..preop.router import _LOGO_URL, _identity, _logo_version, firma_previa_diferida, tiempos_diferido
from . import service
from .catalogo import TipoGasto
from .schemas import GastosOperativosIn, GastosOperativosOut

logger = logging.getLogger(__name__)
router = APIRouter()
_HTML = Path(__file__).resolve().parent.parent / "static" / "gastos_operativos.html"


@dataclass
class GastosOperativosServices:
    """Dependencias del módulo (se cuelgan de ``app.state.gastos_operativos`` en el arranque)."""

    db: Database
    bot_token: str
    max_age_seconds: int
    tipos: tuple[TipoGasto, ...] = field(default_factory=tuple)
    wake_worker: Any = None
    # async (recibido): pide la ubicación en el chat cuando se acepta un envío nuevo.
    on_accepted: Any = None
    # Topes por tipo en COP (``GASTOS_OPERATIVOS_TOPES``): un gasto que los supera se rechaza con 422.
    topes: dict[str, int] = field(default_factory=dict)
    # async (identity, mensaje): avisa al conductor por el chat cuando se rechaza un envío SIN SEÑAL por un tope.
    on_rejected: Any = None


def _services(request: Request) -> GastosOperativosServices:
    svc = getattr(request.app.state, "gastos_operativos", None)
    if svc is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Gastos Operativos no disponible")
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


@router.get("/gastos-operativos", response_class=HTMLResponse, include_in_schema=False)
def gastos_operativos_page() -> HTMLResponse:
    html = _HTML.read_text(encoding="utf-8").replace(f'"{_LOGO_URL}"', f'"{_LOGO_URL}?v={_logo_version()}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@router.get("/api/gastos-operativos/form")
async def gastos_operativos_form(request: Request, _identity: Identity) -> dict[str, Any]:
    svc = _services(request)
    return service.form_full(svc.tipos, svc.topes)


@router.get("/api/gastos-operativos/context")
async def gastos_operativos_context(request: Request, identity: Identity) -> dict[str, Any]:
    svc = _services(request)
    empleado = await asyncio.to_thread(preop_service.employee_for_telegram, svc.db, identity.user_id)
    if empleado is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Tu ID de Telegram ({identity.user_id}) no está registrado como PIN de un empleado activo",
        )
    return await asyncio.to_thread(service.form_context, svc.db, empleado, identity.user_id)


def _out(recibido: service.GastoRecibido, duplicated: bool = False) -> GastosOperativosOut:
    return GastosOperativosOut(id=recibido.id, uuid=recibido.uuid, sync_status=recibido.sync_status.value,
                               total=recibido.total, gastos=recibido.gastos, duplicado=duplicated)


@router.post("/api/gastos-operativos", status_code=status.HTTP_202_ACCEPTED, response_model=GastosOperativosOut,
             dependencies=[Depends(_firma_previa)])
async def gastos_operativos_submit(
    body: GastosOperativosIn,
    request: Request,
    x_telegram_init_data: Annotated[str | None, Header()] = None,
) -> GastosOperativosOut:
    """Guarda y responde de inmediato (202). El envío a Odoo lo hace el worker tras recibir la ubicación."""
    svc = _services(request)
    try:
        if body.diferido is None:
            identity = _identity(x_telegram_init_data, svc.bot_token, svc.max_age_seconds)
            recibido, duplicated = await asyncio.to_thread(
                service.submit, svc.db, identity, body, svc.tipos, None, svc.topes)
        else:
            identity, tiempos = tiempos_diferido(body.diferido, x_telegram_init_data, svc.bot_token,
                                                 svc.max_age_seconds)
            recibido, duplicated = await asyncio.to_thread(
                service.submit_diferido, svc.db, identity, body, tiempos, svc.tipos, svc.topes)
    except preop_service.EnvioVencidoError as exc:
        raise HTTPException(status.HTTP_410_GONE, "Envío vencido: superó 1 hora sin señal") from exc
    except service.TopeExcedidoError as exc:
        if body.diferido is not None:
            # El tope cambió mientras el teléfono estaba sin señal: el formulario ya no está en pantalla, así que
            # además del 422 (que la Mini App muestra al reabrir) se avisa por el chat.
            logger.warning("Gastos operativos diferido RECHAZADO por tope uuid=%s usuario=%s: %s",
                           body.uuid, identity.user_id, exc)
            if svc.on_rejected is not None:
                try:
                    await svc.on_rejected(identity.user_id, str(exc))
                except Exception:
                    logger.exception("No se pudo avisar del rechazo por tope al usuario %s", identity.user_id)
        raise HTTPException(exc.status_code, str(exc)) from exc
    except preop_service.SubmissionError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    if not duplicated and svc.wake_worker is not None:
        svc.wake_worker()
    if not duplicated and svc.on_accepted is not None:
        try:
            await svc.on_accepted(recibido)
        except Exception:  # ya está guardado: el aviso fallido no debe devolver error al conductor
            logger.exception("No se pudo pedir la ubicación de los gastos operativos %s", recibido.id)
    logger.info("Gastos operativos recibidos id=%s uuid=%s usuario=%s gastos=%s duplicado=%s diferido=%s",
                recibido.id, recibido.uuid, identity.user_id, recibido.gastos, duplicated,
                body.diferido is not None)
    return _out(recibido, duplicated)


@router.get("/api/gastos-operativos/{gasto_uuid}", response_model=GastosOperativosOut)
async def gastos_operativos_status(gasto_uuid: str, request: Request, identity: Identity) -> GastosOperativosOut:
    svc = _services(request)

    def _load() -> service.GastoRecibido | None:
        with svc.db.session() as session:
            cab = session.scalar(select(GastoOperativo).where(
                GastoOperativo.uuid == gasto_uuid.lower(), GastoOperativo.telegram_user_id == identity.user_id))
            if cab is None:
                return None
            n = session.scalar(select(func.count()).select_from(GastoOperativoLinea).where(
                GastoOperativoLinea.gasto_id == cab.id)) or 0
            return service.resumen_recibido(cab, int(n))

    recibido = await asyncio.to_thread(_load)
    if recibido is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    return _out(recibido)
