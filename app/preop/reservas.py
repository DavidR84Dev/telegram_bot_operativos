"""Reserva del vehículo durante la jornada (tabla temporal ``reservas_vehiculo``).

Reglas (definidas con el usuario el 2026-11-23):

* Al enviar un preoperacional, el vehículo queda reservado al conductor (su id de
  Telegram y su empleado). Desde el 2026-11-24 solo se puede hacer el preoperacional de un
  vehículo **disponible**: tampoco puede repetirlo el mismo conductor hasta que finalice.
* Una placa tiene un solo conductor a la vez: los demás conductores (aunque figuren
  en el mismo extracto) la ven como NO DISPONIBLE y el servidor rechaza su envío.
* Un conductor tiene un solo vehículo reservado a la vez: debe finalizar antes de
  hacer el preoperacional de otro.
* La reserva se libera con 🔴 Finalizar (borra la del id de Telegram que lo toca y
  avisa al jefe inmediato) o, automáticamente, ``RESERVA_HORAS`` después de creada.
* Toda reserva liberada se conserva en ``reservas_historial`` (finalizar, vencida, manual).

Uso manual (operación)::

    python -m app.preop.reservas --listar
    python -m app.preop.reservas --liberar ABC123
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import Database
from .models import Empleado, Flota, Preoperacional, ReservaHistorial, ReservaVehiculo, utc_now

logger = logging.getLogger(__name__)

# Colombia no tiene horario de verano: UTC-5 fijo (sin depender de tzdata en la imagen).
ZONA_JORNADA = timezone(timedelta(hours=-5), "America/Bogota")
# Vencimiento automático, contado desde el registro inicial de la reserva.
RESERVA_HORAS = 24

DISPONIBLE = "disponible"
PROPIA = "propia"            # reservado por el mismo conductor -> NO DISPONIBLE (ya hizo su preoperacional)
OCUPADO = "ocupado"          # reservado por otro conductor -> NO DISPONIBLE
OTRO_PROPIO = "otro_propio"  # libre, pero el conductor ya tiene otro vehículo -> NO DISPONIBLE

MOTIVO_FINALIZAR = "finalizar"
MOTIVO_VENCIDA = "vencida"
MOTIVO_MANUAL = "manual"


class VehiculoReservadoError(Exception):
    """El vehículo no se puede reservar (otro conductor lo tiene o el conductor ya tiene otro)."""


def fecha_jornada(now: datetime | None = None) -> date:
    return (now or utc_now()).astimezone(ZONA_JORNADA).date()


def reservado_desde(reserva: ReservaVehiculo) -> datetime:
    """``reservado_at`` con zona UTC (SQLite la devuelve sin zona)."""
    return _aware(reserva.reservado_at)


def vence_at(reserva: ReservaVehiculo) -> datetime:
    return _aware(reserva.reservado_at) + timedelta(hours=RESERVA_HORAS)


def _aware(value: datetime) -> datetime:
    """SQLite devuelve fechas sin zona; en la BD siempre se guardan en UTC."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def placa(vehiculo: Flota) -> str:
    return vehiculo.matricula or vehiculo.nombre


# ──────────────────────────────── liberación e histórico ────────────────────────────────

def _contar_preops(session: Session, reserva: ReservaVehiculo, hasta: datetime) -> int:
    return session.scalar(select(func.count()).select_from(Preoperacional).where(
        Preoperacional.telegram_user_id == reserva.telegram_user_id,
        Preoperacional.vehiculo_id == reserva.vehiculo_id,
        Preoperacional.fecha >= reserva.reservado_at,
        Preoperacional.fecha <= hasta,
    )) or 0


def _liberar(session: Session, reserva: ReservaVehiculo, motivo: str, liberado_at: datetime) -> ReservaHistorial:
    """Mueve la reserva al histórico y la borra de la tabla temporal."""
    historial = ReservaHistorial(
        vehiculo_id=reserva.vehiculo_id, placa=placa(reserva.vehiculo), empleado_id=reserva.empleado_id,
        conductor=reserva.empleado.nombre, telegram_user_id=reserva.telegram_user_id,
        extracto_id=reserva.extracto_id, ultimo_preop_id=reserva.preop_id,
        preoperacionales=_contar_preops(session, reserva, liberado_at), fecha_jornada=reserva.fecha_jornada,
        reservado_at=reserva.reservado_at, ultimo_preop_at=reserva.ultimo_preop_at,
        liberado_at=liberado_at, motivo=motivo,
    )
    session.add(historial)
    session.delete(reserva)
    session.flush()
    return historial


def liberar_vencidas(session: Session, now: datetime | None = None) -> int:
    """Libera las reservas con más de ``RESERVA_HORAS`` (se llama antes de cada consulta)."""
    now = now or utc_now()
    limite = now - timedelta(hours=RESERVA_HORAS)
    vencidas = session.scalars(select(ReservaVehiculo).where(ReservaVehiculo.reservado_at <= limite)).all()
    for reserva in vencidas:
        _liberar(session, reserva, MOTIVO_VENCIDA, vence_at(reserva))
        logger.info("Reserva de %s (%s) vencida tras %d h", placa(reserva.vehiculo), reserva.empleado.nombre,
                    RESERVA_HORAS)
    return len(vencidas)


# ─────────────────────────────── consultas y validación ─────────────────────────────────

def estado_reservas(session: Session, vehiculo_ids: list[int], empleado_id: int,
                    now: datetime | None = None) -> dict[int, dict[str, str | None]]:
    """Estado de cada vehículo (id local) para la Mini App.

    ``disponible``, ``propia`` (lo tiene este conductor), ``ocupado`` (lo tiene otro) u
    ``otro_propio`` (libre, pero este conductor ya tiene otro vehículo reservado).
    """
    liberar_vencidas(session, now)
    propia = session.scalar(select(ReservaVehiculo).where(ReservaVehiculo.empleado_id == empleado_id))
    reservas = {r.vehiculo_id: r for r in session.scalars(
        select(ReservaVehiculo).where(ReservaVehiculo.vehiculo_id.in_(vehiculo_ids)))} if vehiculo_ids else {}
    estado: dict[int, dict[str, str | None]] = {}
    for vid in vehiculo_ids:
        r = reservas.get(vid)
        if r is not None and r.empleado_id == empleado_id:
            estado[vid] = {"estado": PROPIA, "conductor": r.empleado.nombre, "placa_propia": None}
        elif r is not None:
            estado[vid] = {"estado": OCUPADO, "conductor": r.empleado.nombre, "placa_propia": None}
        elif propia is not None:
            estado[vid] = {"estado": OTRO_PROPIO, "conductor": None, "placa_propia": placa(propia.vehiculo)}
        else:
            estado[vid] = {"estado": DISPONIBLE, "conductor": None, "placa_propia": None}
    return estado


def verificar_disponible(session: Session, vehiculo: Flota, telegram_user_id: int,
                         now: datetime | None = None) -> None:
    """Lanza ``VehiculoReservadoError`` si el vehículo no está **disponible**.

    Regla del 2026-11-24: solo se hace el preoperacional de un vehículo sin reserva. Si ya
    está reservado (por otro conductor o por el mismo, que ya hizo su preoperacional), o el
    conductor ya tiene otro vehículo, se rechaza hasta que toque 🔴 Finalizar.
    """
    liberar_vencidas(session, now)
    reserva = session.scalar(select(ReservaVehiculo).where(ReservaVehiculo.vehiculo_id == vehiculo.id))
    if reserva is not None and reserva.telegram_user_id == telegram_user_id:
        raise VehiculoReservadoError(
            f"Ya hiciste el preoperacional del vehículo {placa(vehiculo)}: está reservado a tu nombre. "
            "Toca 🔴 Finalizar en el chat al terminar tu jornada.")
    if reserva is not None:
        raise VehiculoReservadoError(
            f"El vehículo {placa(vehiculo)} no está disponible: lo tiene {reserva.empleado.nombre}, que aún no ha "
            "finalizado su jornada.")
    propia = session.scalar(select(ReservaVehiculo).where(ReservaVehiculo.telegram_user_id == telegram_user_id))
    if propia is not None and propia.vehiculo_id != vehiculo.id:
        raise VehiculoReservadoError(
            f"Ya tienes reservado el vehículo {placa(propia.vehiculo)}. Toca 🔴 Finalizar en el chat para "
            "liberarlo antes de hacer el preoperacional de otro vehículo.")


def reservar(session: Session, *, vehiculo: Flota, empleado: Empleado, telegram_user_id: int,
             extracto_id: int | None, preop_id: int, now: datetime | None = None) -> ReservaVehiculo:
    """Crea la reserva del vehículo (``verificar_disponible`` ya comprobó que estaba libre).

    Si ya existiera del mismo conductor solo se actualiza, sin extender el vencimiento.
    """
    now = now or utc_now()
    reserva = session.scalar(select(ReservaVehiculo).where(ReservaVehiculo.vehiculo_id == vehiculo.id))
    if reserva is None:
        reserva = ReservaVehiculo(vehiculo_id=vehiculo.id, empleado_id=empleado.id, telegram_user_id=telegram_user_id,
                                  fecha_jornada=fecha_jornada(now), reservado_at=now)
        session.add(reserva)
    reserva.extracto_id, reserva.preop_id, reserva.ultimo_preop_at = extracto_id, preop_id, now
    return reserva


# ─────────────────── reserva vigente: requisito de los formularios de la jornada ───────────────────

# Regla del 2026-11-26: AST, Prueba de Ruta, Combustible, Gastos Operativos y Pos-Operacional solo se usan con
# una reserva vigente (preoperacional de la jornada). Mismo código HTTP y cabecera ``X-Error-Code`` en todos.
HTTP_SIN_RESERVA = 409
CODIGO_SIN_RESERVA = "sin_reserva_vigente"
MSG_SIN_RESERVA = "Para usar este formulario primero debes registrar el ✅ Preoperacional de hoy (vehículo reservado)."


def reserva_vigente(session: Session, telegram_user_id: int, now: datetime | None = None) -> ReservaVehiculo | None:
    """Reserva del conductor: existe, no se liberó con 🔴 Finalizar y no venció (``RESERVA_HORAS``).

    No exige que la fecha sea hoy: un turno nocturno hace su pos-operacional pasada la medianoche.
    """
    liberar_vencidas(session, now)
    return session.scalar(select(ReservaVehiculo).where(ReservaVehiculo.telegram_user_id == telegram_user_id))


def reserva_al_instante(session: Session, telegram_user_id: int, instante: datetime) -> ReservaVehiculo | None:
    """Reserva que el conductor tenía en ``instante`` aunque ya se haya liberado (envío diferido sin señal).

    Se busca en ``reservas_historial`` la jornada que cubría ese instante y se devuelve una ``ReservaVehiculo``
    **transitoria** (no se agrega a la sesión) con vehículo, extracto y preoperacional de esa jornada.
    """
    fila = session.scalar(
        select(ReservaHistorial).where(
            ReservaHistorial.telegram_user_id == telegram_user_id,
            ReservaHistorial.vehiculo_id.is_not(None),
            ReservaHistorial.reservado_at <= instante,
            ReservaHistorial.liberado_at >= instante,
        ).order_by(ReservaHistorial.liberado_at.desc()).limit(1))
    vehiculo = session.get(Flota, fila.vehiculo_id) if fila is not None and fila.vehiculo_id else None
    if fila is None or vehiculo is None:
        return None
    return ReservaVehiculo(vehiculo_id=vehiculo.id, empleado_id=fila.empleado_id, telegram_user_id=telegram_user_id,
                           extracto_id=fila.extracto_id, preop_id=fila.ultimo_preop_id,
                           fecha_jornada=fila.fecha_jornada, reservado_at=fila.reservado_at,
                           ultimo_preop_at=fila.ultimo_preop_at, vehiculo=vehiculo)


def es_conflicto_de_reserva(exc: Exception) -> bool:
    """Una carrera entre dos envíos choca con un índice único de ``reservas_vehiculo``."""
    text = str(getattr(exc, "orig", exc))
    return "reservas_vehiculo" in text or "uq_reservas_vehiculo" in text


# ──────────────────────────────────── 🔴 Finalizar ──────────────────────────────────────

@dataclass(frozen=True)
class Liberacion:
    placas: tuple[str, ...]
    conductor: str | None = None
    reservado_at: datetime | None = None
    liberado_at: datetime | None = None
    preoperacionales: int = 0
    jefe_telegram_id: int | None = None


def reservas_de(db: Database, telegram_user_id: int) -> tuple[str, ...]:
    """Placa reservada por el id de Telegram (para el mensaje de confirmación)."""
    with db.session() as session:
        liberar_vencidas(session)
        reservas = session.scalars(
            select(ReservaVehiculo).where(ReservaVehiculo.telegram_user_id == telegram_user_id)).all()
        return tuple(placa(r.vehiculo) for r in reservas)


def finalizar_jornada(db: Database, telegram_user_id: int, now: datetime | None = None) -> Liberacion:
    """🔴 Finalizar: pasa la reserva del id de Telegram al histórico y la borra.

    Devuelve los datos para confirmar al conductor y avisar a su jefe inmediato (si el
    jefe tiene su id de Telegram en el PIN).
    """
    from .service import jefe_inmediato  # import diferido: service importa este módulo

    now = now or utc_now()
    with db.session() as session:
        liberar_vencidas(session, now)
        reserva = session.scalar(select(ReservaVehiculo).where(ReservaVehiculo.telegram_user_id == telegram_user_id))
        if reserva is None:
            return Liberacion(())
        empleado = reserva.empleado
        jefe = jefe_inmediato(session, empleado)
        pin = jefe.codigo_pin if jefe else None
        historial = _liberar(session, reserva, MOTIVO_FINALIZAR, now)
        return Liberacion(
            placas=(historial.placa,), conductor=historial.conductor, reservado_at=_aware(historial.reservado_at),
            liberado_at=now, preoperacionales=historial.preoperacionales,
            jefe_telegram_id=int(pin) if pin and pin.isdigit() else None,
        )


def liberar_placa(db: Database, matricula: str) -> int:
    """Operación manual: libera una placa (queda en el histórico con motivo ``manual``)."""
    with db.session() as session:
        reservas = session.scalars(select(ReservaVehiculo).join(Flota, ReservaVehiculo.vehiculo_id == Flota.id).where(
            func.upper(Flota.matricula) == matricula.strip().upper())).all()
        for reserva in reservas:
            _liberar(session, reserva, MOTIVO_MANUAL, utc_now())
        return len(reservas)


def main() -> None:  # pragma: no cover - CLI
    parser = argparse.ArgumentParser(description="Reservas de vehículos del preoperacional")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--listar", action="store_true", help="muestra las reservas vigentes")
    group.add_argument("--liberar", metavar="PLACA", help="libera la reserva de una placa")
    args = parser.parse_args()
    from ..config import get_settings

    db = Database(get_settings().database_url)
    if args.liberar:
        print(f"Reservas liberadas: {liberar_placa(db, args.liberar)}")
        return
    with db.session() as session:
        liberar_vencidas(session)
        for r in session.scalars(select(ReservaVehiculo).order_by(ReservaVehiculo.reservado_at)):
            print(f"{placa(r.vehiculo):10} {r.empleado.nombre:40} jornada={r.fecha_jornada} "
                  f"desde={r.reservado_at:%Y-%m-%d %H:%M} vence={vence_at(r):%Y-%m-%d %H:%M}")


if __name__ == "__main__":  # pragma: no cover
    main()
