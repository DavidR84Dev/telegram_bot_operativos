"""Endpoints del Panel Admin (Mini App ``/admin``).

TODAS las rutas ``/api/admin/*`` exigen, por la dependencia ``AdminIdentity``:

1. ``X-Telegram-Init-Data`` con firma HMAC-SHA256 válida **del token del bot admin** (401 si no), y
2. que el id de Telegram esté en ``ADMIN_TELEGRAM_IDS`` (403 si no; lista vacía = nadie).

Cada acción que cambia algo deja una fila en ``admin_auditoria``.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..combustible import proveedores
from ..preop import service as preop_service
from ..preop.db import Database
from ..preop.router import _LOGO_URL, _identity, _logo_version
from . import audit, envfile, service

router = APIRouter()
_HTML = Path(__file__).resolve().parent.parent / "static" / "admin.html"
CSV_MAX = 2_000_000


@dataclass
class AdminServices:
    """Dependencias del panel (se cuelgan de ``app.state.admin`` en el arranque)."""

    db: Database
    bot_token: str
    admin_ids: frozenset[int]
    env_file: Path
    max_age_seconds: int = 14400


def _services(request: Request) -> AdminServices:
    svc = getattr(request.app.state, "admin", None)
    if svc is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Panel no disponible")
    return svc


def admin_identity(
    request: Request,
    x_telegram_init_data: Annotated[str | None, Header()] = None,
) -> preop_service.TelegramIdentity:
    svc = _services(request)
    identity = _identity(x_telegram_init_data, svc.bot_token, svc.max_age_seconds)  # 401 si la firma no vale
    if identity.user_id not in svc.admin_ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Tu usuario de Telegram no es administrador del panel")
    return identity


AdminIdentity = Annotated[preop_service.TelegramIdentity, Depends(admin_identity)]


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def admin_page() -> HTMLResponse:
    html = _HTML.read_text(encoding="utf-8").replace(f'"{_LOGO_URL}"', f'"{_LOGO_URL}?v={_logo_version()}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@router.get("/api/admin/me")
def me(identity: AdminIdentity) -> dict[str, Any]:
    return {"user_id": identity.user_id, "nombre": identity.full_name}


# ───────────────────────────────────────── 1. Vehículos (reservas) ─────────────────────────────────────

@router.get("/api/admin/reservas")
async def reservas_list(request: Request, _identity: AdminIdentity, q: str = "") -> dict[str, Any]:
    db = _services(request).db

    def _load() -> dict[str, Any]:
        with db.session() as session:
            return service.listar_reservas(session, q)

    return await asyncio.to_thread(_load)


@router.post("/api/admin/reservas/{reserva_id}/liberar")
async def reservas_liberar(reserva_id: int, request: Request, identity: AdminIdentity) -> dict[str, Any]:
    db = _services(request).db

    def _run() -> dict[str, Any] | None:
        with db.session() as session:
            historial = service.liberar_reserva(session, reserva_id)
            if historial is None:
                return None
            audit.registrar(session, identity, "reserva.liberar", f"reserva {reserva_id} · {historial.placa}",
                            f"conductor={historial.conductor}; telegram_id={historial.telegram_user_id}; "
                            f"historial_id={historial.id}")
            return {"ok": True, "historial_id": historial.id, "placa": historial.placa}

    out = await asyncio.to_thread(_run)
    if out is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "La reserva ya no existe (¿ya fue liberada?)")
    return out


# ───────────────────────────────────────── 2. Empleados y flota ─────────────────────────────────────────

@router.get("/api/admin/empleados")
async def empleados_buscar(request: Request, _identity: AdminIdentity, q: str = "") -> dict[str, Any]:
    db = _services(request).db

    def _load() -> list[dict[str, Any]]:
        with db.session() as session:
            return service.buscar_empleados(session, q)

    return {"items": await asyncio.to_thread(_load)}


@router.get("/api/admin/flota")
async def flota_buscar(request: Request, _identity: AdminIdentity, q: str = "") -> dict[str, Any]:
    db = _services(request).db

    def _load() -> list[dict[str, Any]]:
        with db.session() as session:
            return service.buscar_flota(session, q)

    return {"items": await asyncio.to_thread(_load)}


# ─────────────────────────────────────────── 3. Usuarios registrados ───────────────────────────────────

@router.get("/api/admin/usuarios")
async def usuarios_list(request: Request, _identity: AdminIdentity, q: str = "") -> dict[str, Any]:
    db = _services(request).db

    def _load() -> dict[str, Any]:
        with db.session() as session:
            return service.listar_usuarios(session, q)

    return await asyncio.to_thread(_load)


@router.delete("/api/admin/usuarios/{usuario_id}")
async def usuarios_eliminar(usuario_id: int, request: Request, identity: AdminIdentity) -> dict[str, Any]:
    """Borra solo la fila local. El PIN de Odoo no se toca: si sigue allí, la persona conserva el acceso."""
    db = _services(request).db

    def _run() -> dict[str, Any] | None:
        with db.session() as session:
            fila = service.eliminar_usuario(session, usuario_id)
            if fila is None:
                return None
            audit.registrar(session, identity, "usuario.eliminar", f"registro {usuario_id} · {fila.nombre_completo}",
                            f"telegram_id={fila.telegram_user_id}; estado={fila.estado}; "
                            f"empleado_odoo_id={fila.empleado_odoo_id}; solo fila local (Odoo no se modificó)")
            return {"ok": True, "telegram_user_id": fila.telegram_user_id}

    out = await asyncio.to_thread(_run)
    if out is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "El registro ya no existe")
    return out


# ──────────────────────────────────────────── 4. Estaciones ────────────────────────────────────────────

class CsvIn(BaseModel):
    csv: str = Field(max_length=CSV_MAX)
    reemplazar: bool = False


class ActivoIn(BaseModel):
    activo: bool


@router.get("/api/admin/estaciones")
async def estaciones_list(request: Request, _identity: AdminIdentity, q: str = "",
                          estado: str = Query("", pattern="^(|activa|inactiva)$")) -> dict[str, Any]:
    db = _services(request).db

    def _load() -> dict[str, Any]:
        with db.session() as session:
            return service.listar_estaciones(session, q, estado)

    return await asyncio.to_thread(_load)


def _analizar(body: CsvIn) -> tuple[list[tuple[int, proveedores.FilaProveedor]], list[proveedores.ErrorFila]]:
    try:
        return proveedores.analizar_csv(body.csv)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/api/admin/estaciones/previsualizar")
async def estaciones_previsualizar(body: CsvIn, request: Request, _identity: AdminIdentity) -> dict[str, Any]:
    """Vista previa del CSV: filas válidas, errores por fila, nuevos y actualizados. No escribe nada."""
    db = _services(request).db
    validas, errores = _analizar(body)
    filas = [f for _, f in validas]
    resumen = await asyncio.to_thread(proveedores.previsualizar, db, filas, body.reemplazar)
    return {
        "validas": len(filas), "errores": [{"linea": e.linea, "mensaje": e.mensaje} for e in errores],
        "reemplazar": body.reemplazar, "bloquea_reemplazo": bool(body.reemplazar and errores), **resumen,
        "muestra": [{"linea": n, "nombre": f.nombre, "nit": f.nit, "latitud": f.latitud, "longitud": f.longitud,
                     "ciudad": f.ciudad, "direccion": f.direccion} for n, f in validas[:50]],
    }


@router.post("/api/admin/estaciones/cargar")
async def estaciones_cargar(body: CsvIn, request: Request, identity: AdminIdentity) -> dict[str, Any]:
    """Confirma la carga. Con «reemplazar» no se admiten errores: faltarían filas y se desactivarían de más."""
    db = _services(request).db
    validas, errores = _analizar(body)
    if not validas:
        raise HTTPException(422, "El archivo no tiene filas válidas")
    if body.reemplazar and errores:
        raise HTTPException(422,
                            "Con «reemplazar» el archivo no puede tener errores: corrígelos y vuelve a subirlo")
    resumen = await asyncio.to_thread(proveedores.cargar, db, [f for _, f in validas], body.reemplazar)

    def _audit() -> None:
        with db.session() as session:
            audit.registrar(session, identity, "estaciones.cargar", "proveedores_combustible",
                            f"nuevos={resumen.nuevos}; actualizados={resumen.actualizados}; "
                            f"desactivados={resumen.desactivados}; filas_con_error={len(errores)}; "
                            f"reemplazar={body.reemplazar}")

    await asyncio.to_thread(_audit)
    return {"nuevos": resumen.nuevos, "actualizados": resumen.actualizados, "desactivados": resumen.desactivados,
            "omitidas_por_error": len(errores), "sin_contacto": len(resumen.sin_contacto)}


@router.post("/api/admin/estaciones/{estacion_id}/activo")
async def estaciones_activo(estacion_id: int, body: ActivoIn, request: Request,
                            identity: AdminIdentity) -> dict[str, Any]:
    db = _services(request).db

    def _run() -> str | None:
        nombre = proveedores.set_activo(db, estacion_id, body.activo)
        if nombre is not None:
            with db.session() as session:
                audit.registrar(session, identity, "estacion.activar" if body.activo else "estacion.desactivar",
                                f"estación {estacion_id} · {nombre}")
        return nombre

    nombre = await asyncio.to_thread(_run)
    if nombre is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "La estación no existe")
    return {"ok": True, "activo": body.activo}


# ──────────────────────────────────────────── 5. Parámetros (.env) ─────────────────────────────────────

class ParametrosIn(BaseModel):
    cambios: dict[str, str | int | float] = Field(min_length=1, max_length=len(envfile.PARAMETROS))


@router.get("/api/admin/parametros")
async def parametros_list(request: Request, _identity: AdminIdentity) -> dict[str, Any]:
    ruta = _services(request).env_file
    try:
        items = await asyncio.to_thread(envfile.listar, ruta)
    except envfile.ParametroError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return {"items": items, "aviso": "Los cambios aplican al reiniciar el contenedor de Operativos "
                                     "(docker compose restart app)."}


@router.put("/api/admin/parametros")
async def parametros_guardar(body: ParametrosIn, request: Request, identity: AdminIdentity) -> dict[str, Any]:
    svc = _services(request)
    try:
        cambiadas, respaldo = await asyncio.to_thread(envfile.guardar, svc.env_file, dict(body.cambios))
    except envfile.ParametroError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    def _audit() -> None:
        with svc.db.session() as session:
            for clave in cambiadas:  # solo la clave: nunca el valor (puede ser un secreto)
                audit.registrar(session, identity, "parametro.cambiar", clave,
                                "secreto: valor no registrado" if envfile.PARAMETROS[clave].secreto else None)

    if cambiadas:
        await asyncio.to_thread(_audit)
    return {"cambiados": cambiadas, "respaldo": respaldo.name if respaldo else None,
            "aviso": "Los cambios aplican al reiniciar el contenedor de Operativos (docker compose restart app)."}


# ─────────────────────────────────────────────── Auditoría ─────────────────────────────────────────────

@router.get("/api/admin/auditoria")
async def auditoria_list(request: Request, _identity: AdminIdentity,
                         limite: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    db = _services(request).db

    def _load() -> list[dict]:
        with db.session() as session:
            return audit.listar(session, limite)

    return {"items": await asyncio.to_thread(_load)}
