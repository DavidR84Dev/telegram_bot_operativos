"""Consultas y acciones del Panel Admin sobre las tablas de Operativos (sin tocar Odoo)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..preop import reservas
from ..preop.models import (
    Empleado,
    Flota,
    ProveedorCombustible,
    RegistroConductor,
    ReservaHistorial,
    ReservaVehiculo,
    utc_now,
)

LIMITE = 200


def iso(valor: datetime | None) -> str | None:
    return reservas._aware(valor).isoformat() if valor else None


def _num(q: str) -> int | None:
    q = q.strip()
    return int(q) if q.isdigit() and len(q) <= 18 else None


# ─────────────────────────────────────── 1. Vehículos (reservas) ───────────────────────────────────────

def listar_reservas(session: Session, q: str = "") -> dict[str, Any]:
    ahora = utc_now()
    todas = session.scalars(select(ReservaVehiculo)).all()
    inicio_hoy = datetime.combine(reservas.fecha_jornada(ahora), datetime.min.time(),
                                  tzinfo=reservas.ZONA_JORNADA).astimezone(timezone.utc)
    liberadas_hoy = session.scalar(
        select(func.count()).select_from(ReservaHistorial).where(ReservaHistorial.liberado_at >= inicio_hoy)) or 0
    kpis = {"activas": len(todas), "vencidas": sum(1 for r in todas if reservas.vence_at(r) <= ahora),
            "liberadas_hoy": liberadas_hoy}

    q = q.strip()
    filas = sorted(todas, key=lambda r: reservas._aware(r.reservado_at), reverse=True)
    if q:
        n = _num(q)
        ql = q.lower()

        def coincide(r: ReservaVehiculo) -> bool:
            textos = (r.empleado.nombre, r.vehiculo.nombre, r.vehiculo.matricula or "", r.vehiculo.numero_interno or "")
            if any(ql in t.lower() for t in textos):
                return True
            return n is not None and n in (r.id, r.vehiculo.odoo_id, r.empleado.odoo_id, r.telegram_user_id,
                                           r.preop_id)

        filas = [r for r in filas if coincide(r)]
    items = [{
        "id": r.id, "conductor": r.empleado.nombre, "placa": reservas.placa(r.vehiculo),
        "vehiculo": r.vehiculo.nombre, "vehiculo_odoo_id": r.vehiculo.odoo_id,
        "empleado_odoo_id": r.empleado.odoo_id, "telegram_user_id": r.telegram_user_id, "preop_id": r.preop_id,
        "reservado_at": iso(r.reservado_at), "vence_at": iso(reservas.vence_at(r)),
        "vencida": reservas.vence_at(r) <= ahora,
    } for r in filas[:LIMITE]]
    return {"kpis": kpis, "total": len(filas), "items": items}


def liberar_reserva(session: Session, reserva_id: int) -> ReservaHistorial | None:
    """Libera por id de reserva (motivo ``manual``): la fila pasa a ``reservas_historial``."""
    reserva = session.get(ReservaVehiculo, reserva_id)
    if reserva is None:
        return None
    return reservas._liberar(session, reserva, reservas.MOTIVO_MANUAL, utc_now())


# ───────────────────────────────────────── 2. Empleados y flota ─────────────────────────────────────────

def buscar_empleados(session: Session, q: str) -> list[dict[str, Any]]:
    """Solo lectura: nombre, pin/telegram, id Odoo o documento (de los registros de conductor)."""
    q = q.strip()
    if not q:
        return []
    cond = [Empleado.nombre.icontains(q, autoescape=True), Empleado.codigo_pin == q]
    n = _num(q)
    if n is not None:
        cond.append(Empleado.odoo_id == n)
    # La copia local de empleados no guarda el documento: se cruza con los registros de conductor.
    por_documento = select(RegistroConductor.empleado_odoo_id).where(
        RegistroConductor.documento.contains(q, autoescape=True), RegistroConductor.empleado_odoo_id.is_not(None))
    cond.append(Empleado.odoo_id.in_(por_documento))
    empleados = session.scalars(select(Empleado).where(or_(*cond)).order_by(Empleado.nombre).limit(50)).all()
    reservas_por_emp = {r.empleado_id: r for r in session.scalars(
        select(ReservaVehiculo).where(ReservaVehiculo.empleado_id.in_([e.id for e in empleados])))}
    documentos = {d.empleado_odoo_id: d.documento for d in session.scalars(
        select(RegistroConductor).where(RegistroConductor.empleado_odoo_id.in_([e.odoo_id for e in empleados])))}
    out = []
    for e in empleados:
        r = reservas_por_emp.get(e.id)
        out.append({
            "id": e.id, "odoo_id": e.odoo_id, "nombre": e.nombre, "puesto": e.puesto_trabajo,
            "contrato": e.contrato or e.contrato_temporal, "responsable": e.responsable,
            "codigo_pin": e.codigo_pin, "documento": documentos.get(e.odoo_id), "activo": e.activo,
            "vehiculo_reservado": reservas.placa(r.vehiculo) if r else None,
            "reservado_at": iso(r.reservado_at) if r else None,
        })
    return out


def buscar_flota(session: Session, q: str) -> list[dict[str, Any]]:
    q = q.strip()
    if not q:
        return []
    cond = [Flota.nombre.icontains(q, autoescape=True), Flota.matricula.icontains(q, autoescape=True),
            Flota.numero_interno == q]
    n = _num(q)
    if n is not None:
        cond.append(Flota.odoo_id == n)
    vehiculos = session.scalars(select(Flota).where(or_(*cond)).order_by(Flota.nombre).limit(50)).all()
    reservadas = {r.vehiculo_id: r for r in session.scalars(
        select(ReservaVehiculo).where(ReservaVehiculo.vehiculo_id.in_([v.id for v in vehiculos])))}
    return [{
        "id": v.id, "odoo_id": v.odoo_id, "nombre": v.nombre, "placa": v.matricula, "estado": v.estado,
        "numero_interno": v.numero_interno, "parque_automotor": v.parque_automotor, "activo": v.activo,
        "reservado_por": reservadas[v.id].empleado.nombre if v.id in reservadas else None,
    } for v in vehiculos]


# ─────────────────────────────────────────── 3. Usuarios registrados ───────────────────────────────────────

def _usuario(r: RegistroConductor) -> dict[str, Any]:
    return {"id": r.id, "telegram_user_id": r.telegram_user_id, "username": r.telegram_username,
            "nombre": r.nombre_completo, "documento": r.documento, "correo": r.correo, "estado": r.estado,
            "empleado_odoo_id": r.empleado_odoo_id, "intentos": r.intentos,
            "registrado_at": iso(r.registrado_at or r.created_at)}


def listar_usuarios(session: Session, q: str = "") -> dict[str, Any]:
    stmt = select(RegistroConductor)
    q = q.strip()
    if q:
        cond = [RegistroConductor.nombre_completo.icontains(q, autoescape=True),
                RegistroConductor.documento.icontains(q, autoescape=True),
                RegistroConductor.correo.icontains(q, autoescape=True),
                RegistroConductor.telegram_username.icontains(q.lstrip("@"), autoescape=True)]
        n = _num(q)
        if n is not None:
            cond += [RegistroConductor.id == n, RegistroConductor.telegram_user_id == n,
                     RegistroConductor.empleado_odoo_id == n]
        stmt = stmt.where(or_(*cond))
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    filas = session.scalars(stmt.order_by(RegistroConductor.created_at.desc(), RegistroConductor.id.desc())
                            .limit(LIMITE)).all()
    return {"total": total, "items": [_usuario(r) for r in filas]}


def eliminar_usuario(session: Session, usuario_id: int) -> RegistroConductor | None:
    """Borra SOLO la fila local de ``registros_conductor``; no escribe en Odoo (el PIN no se toca)."""
    fila = session.get(RegistroConductor, usuario_id)
    if fila is not None:
        session.delete(fila)
        session.flush()
    return fila


# ──────────────────────────────────────────── 4. Estaciones ────────────────────────────────────────────

def listar_estaciones(session: Session, q: str = "", estado: str = "") -> dict[str, Any]:
    stmt = select(ProveedorCombustible)
    q = q.strip()
    if q:
        stmt = stmt.where(or_(ProveedorCombustible.nombre.icontains(q, autoescape=True),
                              ProveedorCombustible.ciudad.icontains(q, autoescape=True),
                              ProveedorCombustible.nit == q))
    if estado in ("activa", "inactiva"):
        stmt = stmt.where(ProveedorCombustible.activo.is_(estado == "activa"))
    filas = session.scalars(stmt.order_by(ProveedorCombustible.nombre)).all()
    activas = session.scalar(select(func.count()).select_from(ProveedorCombustible).where(
        ProveedorCombustible.activo.is_(True))) or 0
    con_ubicacion = session.scalar(select(func.count()).select_from(ProveedorCombustible).where(and_(
        ProveedorCombustible.activo.is_(True), ProveedorCombustible.latitud.is_not(None)))) or 0
    return {
        "kpis": {"activas": activas, "con_ubicacion": con_ubicacion},
        "total": len(filas),
        "items": [{"id": p.id, "nombre": p.nombre, "nit": p.nit, "odoo_partner_id": p.odoo_partner_id,
                   "activo": p.activo, "latitud": p.latitud, "longitud": p.longitud, "ciudad": p.ciudad,
                   "direccion": p.direccion} for p in filas[:1000]],
    }
