"""Endpoints de la Mini App del preoperacional.

Seguridad: todas las rutas ``/api/preop/*`` exigen la cabecera
``X-Telegram-Init-Data`` (``Telegram.WebApp.initData``) con firma HMAC-SHA256
válida del bot y antigüedad acotada. La identidad sale de ahí, nunca del cuerpo.

* ``GET  /preoperacional``        página HTML de la Mini App (pública; no tiene datos)
* ``GET  /static/viacotur-logo.png`` logo de la cabecera (público)
* ``GET  /api/preop/form``        catálogo de ítems (secciones, opciones, medidas)
* ``GET  /api/preop/context``     conductor, responsable y extractos aprobados con su vehículo
* ``POST /api/preop``             recibe el formulario -> 202 + estado ``pendiente``
  (reenvío sin señal fuera de la ventana de 1 h -> 410, solo auditoría)
* ``GET  /api/preop/{uuid}``      estado de sincronización de un envío propio
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import select

from ..webapp_auth import InitDataError, init_data_auth_date, validate_init_data_user
from . import diferido, service
from .checklist import form_definition
from .db import Database
from .models import Preoperacional, utc_now
from .schemas import EnvioDiferidoIn, PreoperacionalIn, PreoperacionalOut

logger = logging.getLogger(__name__)
router = APIRouter()
_STATIC = Path(__file__).resolve().parent.parent / "static"
_HTML = _STATIC / "preoperacional.html"
_LOGO = _STATIC / "viacotur-logo.png"


@dataclass
class PreopServices:
    """Dependencias del módulo (se cuelgan de ``app.state.preop`` en el arranque)."""

    db: Database
    bot_token: str
    max_age_seconds: int
    wake_worker: Any = None  # callable sin argumentos: despierta el worker tras un envío
    # Lee el último odómetro del vehículo en Odoo (id de Odoo -> km | None). None = solo datos locales.
    odometro: Any = None


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
    return _identity(x_telegram_init_data, svc.bot_token, svc.max_age_seconds)


def _validar(init_data: str | None, bot_token: str, max_age_seconds: int) -> dict:
    try:
        return validate_init_data_user(init_data or "", bot_token, max_age_seconds)
    except InitDataError as exc:
        logger.warning("Preop: initData rechazado (%s); longitud=%d", exc, len(init_data or ""))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sesión de Telegram no válida") from exc


def _identity(init_data: str | None, bot_token: str, max_age_seconds: int) -> service.TelegramIdentity:
    user = _validar(init_data, bot_token, max_age_seconds)
    full_name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p).strip()
    username = user.get("username")
    return service.TelegramIdentity(
        user_id=int(user["id"]),
        username=str(username)[:64] if username else None,
        full_name=full_name or str(user["id"]),
    )


Identity = Annotated[service.TelegramIdentity, Depends(telegram_identity)]


_LOGO_URL = "/static/viacotur-logo.png"


def _logo_version() -> str:
    """Huella del logo: cambia cuando cambia la imagen, así el celular no usa la anterior en caché."""
    return hashlib.sha256(_LOGO.read_bytes()).hexdigest()[:12]


@router.get("/preoperacional", response_class=HTMLResponse, include_in_schema=False)
def preop_page() -> HTMLResponse:
    # La página nunca se guarda en caché (siempre la versión vigente); el logo lleva su versión en la URL.
    html = _HTML.read_text(encoding="utf-8").replace(f'"{_LOGO_URL}"', f'"{_LOGO_URL}?v={_logo_version()}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@router.get(_LOGO_URL, include_in_schema=False)
def logo() -> FileResponse:
    # Con ?v=<huella> la URL cambia al cambiar la imagen, por eso se puede guardar un día en caché.
    return FileResponse(_LOGO, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


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
    return await asyncio.to_thread(service.form_context, svc.db, empleado, None, svc.odometro)


def tiempos_diferido(diferido_in: EnvioDiferidoIn, init_data: str | None, bot_token: str,
                     max_age_seconds: int) -> tuple[service.TelegramIdentity, service.TiemposDiferido]:
    """Autentica un reenvío diferido (de cualquier formulario) y arma sus horas.

    La cabecera puede ser el initData original (el Mini App siguió abierto sin señal) o uno
    nuevo (se volvió a abrir): se acepta hasta el tope de la ventana. El initData original
    solo sirve para fechar la apertura y debe ser del mismo usuario.
    """
    max_age = max(max_age_seconds, int(diferido.TOPE_DESDE_APERTURA.total_seconds()))
    identity = _identity(init_data, bot_token, max_age)
    original = diferido_in.init_data_original
    user = _validar(original, bot_token, int(diferido.ANTIGUEDAD_MAXIMA_AUDITORIA.total_seconds()))
    if int(user["id"]) != identity.user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "El formulario guardado es de otro usuario")
    tiempos = service.TiemposDiferido(
        apertura=datetime.fromtimestamp(init_data_auth_date(original), UTC),
        sin_senal_desde=diferido_in.sin_senal_desde.astimezone(UTC),
        recibido=utc_now(),
    )
    return identity, tiempos


def firma_previa_diferida(init_data: str | None, bot_token: str, max_age_seconds: int) -> None:
    """Rechaza (401) sin firma válida antes de validar el cuerpo. Admite la antigüedad de un
    reenvío sin señal; la antigüedad exacta se revisa en la ruta."""
    _validar(init_data, bot_token, max(max_age_seconds, int(diferido.TOPE_DESDE_APERTURA.total_seconds())))


def _out(preop: Preoperacional, duplicated: bool = False) -> PreoperacionalOut:
    return PreoperacionalOut(
        id=preop.id, uuid=preop.uuid, sync_status=preop.sync_status.value, odoo_id=preop.odoo_id,
        odoo_name=preop.odoo_name, no_conformes=preop.no_conformes, duplicado=duplicated,
    )


def _firma_previa(request: Request, x_telegram_init_data: Annotated[str | None, Header()] = None) -> None:
    """Rechaza (401) sin firma válida **antes** de validar el cuerpo: así un cliente sin sesión
    no ve los errores de validación del formulario."""
    svc = _services(request)
    firma_previa_diferida(x_telegram_init_data, svc.bot_token, svc.max_age_seconds)


@router.post("/api/preop", status_code=status.HTTP_202_ACCEPTED, response_model=PreoperacionalOut,
             dependencies=[Depends(_firma_previa)])
async def preop_submit(
    body: PreoperacionalIn,
    request: Request,
    x_telegram_init_data: Annotated[str | None, Header()] = None,
) -> PreoperacionalOut:
    """Guarda y responde de inmediato (202). El envío a Odoo lo hace el worker en segundo plano.

    Un reenvío de un formulario guardado sin señal (``diferido``) responde 410 si llegó fuera
    de la ventana (queda solo como auditoría; ver ``app/preop/diferido.py``).
    """
    svc = _services(request)
    try:
        if body.diferido is None:
            identity = _identity(x_telegram_init_data, svc.bot_token, svc.max_age_seconds)
            preop, duplicated = await asyncio.to_thread(service.submit, svc.db, identity, body, svc.odometro)
        else:
            assert body.diferido is not None
            identity, tiempos = tiempos_diferido(body.diferido, x_telegram_init_data, svc.bot_token,
                                                 svc.max_age_seconds)
            preop, duplicated = await asyncio.to_thread(
                service.submit_diferido, svc.db, identity, body, tiempos, svc.odometro)
    except service.EnvioVencidoError as exc:
        # Sin aviso al conductor (decisión del 2026-11-24): la Mini App lo descarta en silencio.
        raise HTTPException(status.HTTP_410_GONE, "Envío vencido: superó 1 hora sin señal") from exc
    except service.SubmissionError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    if not duplicated and svc.wake_worker is not None:
        svc.wake_worker()
    logger.info("Preoperacional recibido id=%s uuid=%s usuario=%s duplicado=%s diferido=%s",
                preop.id, preop.uuid, identity.user_id, duplicated, body.diferido is not None)
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
