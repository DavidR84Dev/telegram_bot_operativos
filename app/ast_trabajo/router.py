"""Endpoints de la Mini App del AST.

Seguridad igual que el preoperacional: las rutas ``/api/ast/*`` exigen la cabecera
``X-Telegram-Init-Data`` con firma HMAC-SHA256 válida del bot. La identidad sale de
ahí, nunca del cuerpo.

* ``GET  /ast``              página HTML de la Mini App (pública; no tiene datos)
* ``GET  /api/ast/form``     catálogo de preguntas
* ``GET  /api/ast/context``  empleado, responsable y contratos de sus extractos aprobados
* ``POST /api/ast``          recibe el formulario -> 202 + estado ``pendiente``
* ``GET  /api/ast/{uuid}``   estado de sincronización de un envío propio
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
from ..preop.models import AnalisisSeguro
from ..preop.router import _LOGO_URL, _identity, _logo_version, _validar
from . import service
from .catalogo import form_definition
from .schemas import AstIn, AstOut

logger = logging.getLogger(__name__)
router = APIRouter()
_HTML = Path(__file__).resolve().parent.parent / "static" / "ast.html"


@dataclass
class AstServices:
    """Dependencias del módulo (se cuelgan de ``app.state.ast`` en el arranque)."""

    db: Database
    bot_token: str
    max_age_seconds: int
    wake_worker: Any = None  # callable sin argumentos: despierta el worker tras un envío


def _services(request: Request) -> AstServices:
    svc = getattr(request.app.state, "ast", None)
    if svc is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "AST no disponible")
    return svc


def telegram_identity(
    request: Request,
    x_telegram_init_data: Annotated[str | None, Header()] = None,
) -> preop_service.TelegramIdentity:
    svc = _services(request)
    return _identity(x_telegram_init_data, svc.bot_token, svc.max_age_seconds)


Identity = Annotated[preop_service.TelegramIdentity, Depends(telegram_identity)]


def _firma_previa(request: Request, x_telegram_init_data: Annotated[str | None, Header()] = None) -> None:
    """Rechaza (401) sin firma válida **antes** de validar el cuerpo."""
    svc = _services(request)
    _validar(x_telegram_init_data, svc.bot_token, svc.max_age_seconds)


@router.get("/ast", response_class=HTMLResponse, include_in_schema=False)
def ast_page() -> HTMLResponse:
    html = _HTML.read_text(encoding="utf-8").replace(f'"{_LOGO_URL}"', f'"{_LOGO_URL}?v={_logo_version()}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@router.get("/api/ast/form")
def ast_form(_identity: Identity) -> dict[str, Any]:
    return form_definition()


@router.get("/api/ast/context")
async def ast_context(request: Request, identity: Identity) -> dict[str, Any]:
    svc = _services(request)
    empleado = await asyncio.to_thread(preop_service.employee_for_telegram, svc.db, identity.user_id)
    if empleado is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Tu ID de Telegram ({identity.user_id}) no está registrado como PIN de un empleado activo",
        )
    return await asyncio.to_thread(service.form_context, svc.db, empleado)


def _out(ast: AnalisisSeguro, duplicated: bool = False) -> AstOut:
    return AstOut(id=ast.id, uuid=ast.uuid, sync_status=ast.sync_status.value, odoo_id=ast.odoo_id,
                  odoo_name=ast.odoo_name, no_conformes=ast.no_conformes, duplicado=duplicated)


@router.post("/api/ast", status_code=status.HTTP_202_ACCEPTED, response_model=AstOut,
             dependencies=[Depends(_firma_previa)])
async def ast_submit(body: AstIn, request: Request, identity: Identity) -> AstOut:
    """Guarda y responde de inmediato (202). El envío a Odoo lo hace el worker en segundo plano."""
    svc = _services(request)
    try:
        ast, duplicated = await asyncio.to_thread(service.submit, svc.db, identity, body)
    except preop_service.SubmissionError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    if not duplicated and svc.wake_worker is not None:
        svc.wake_worker()
    logger.info("AST recibido id=%s uuid=%s usuario=%s duplicado=%s alertas=%s",
                ast.id, ast.uuid, identity.user_id, duplicated, ast.no_conformes)
    return _out(ast, duplicated)


@router.get("/api/ast/{ast_uuid}", response_model=AstOut)
async def ast_status(ast_uuid: str, request: Request, identity: Identity) -> AstOut:
    svc = _services(request)

    def _load() -> AnalisisSeguro | None:
        with svc.db.session() as session:
            return session.scalar(select(AnalisisSeguro).where(
                AnalisisSeguro.uuid == ast_uuid.lower(),
                AnalisisSeguro.telegram_user_id == identity.user_id,  # solo envíos propios
            ))

    ast = await asyncio.to_thread(_load)
    if ast is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    return _out(ast)
