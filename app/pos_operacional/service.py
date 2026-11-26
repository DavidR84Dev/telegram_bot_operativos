"""Lógica del Pos-Operacional: contexto, recepción (incluido el envío sin señal) y envío a Odoo (``pos.operacional``).

Reglas:

1. El id de Telegram es el PIN de un empleado activo (si no, 403).
2. El vehículo, el contrato y el KM inicial sugerido salen de la **reserva de la jornada** (la del preoperacional).
   Exige una **reserva vigente** (``reservas.reserva_vigente``): sin ella ``SinReservaVigenteError`` (409). No tiene
   que ser de hoy: un turno nocturno hace su pos-operacional pasada la medianoche.
3. La planilla (PDF, JPG o PNG, validada por su firma) se envía a Odoo **siempre en PDF** (``type_file="pdf"``):
   una foto se convierte a PDF en el servidor.
4. No pide ubicación y **no libera** la reserva: eso lo hace 🔴 Finalizar.

Todo es síncrono (SQLAlchemy + XML-RPC); el código async lo llama con ``asyncio.to_thread``.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..gastos_operativos import soporte as soporte_mod
from ..preop import reservas
from ..preop import service as preop_service
from ..preop.db import Database
from ..preop.diferido import motivo_vencido
from ..preop.models import (
    Acuerdo,
    Empleado,
    EstadoDiferido,
    Extracto,
    PosOperacional,
    PosOperacionalEnvioDiferido,
    Preoperacional,
    ReservaVehiculo,
    SyncStatus,
    utc_now,
)
from ..preop.odoo_writer import OdooPermanentError, OdooTransientError, OdooWriter, _odoo_datetime
from ..preop.service import EnvioVencidoError, SubmissionError, TelegramIdentity, TiemposDiferido, payload_sha256
from .catalogo import form_definition
from .schemas import PosOperacionalIn

logger = logging.getLogger(__name__)

__all__ = ["form_definition"]


def _hora_colombia(valor: datetime) -> str:
    """``YYYY-MM-DDTHH:MM`` en hora de Colombia (formato de ``<input type="datetime-local">``)."""
    return reservas._aware(valor).astimezone(reservas.ZONA_JORNADA).strftime("%Y-%m-%dT%H:%M")


# ───────────────────────────── consultas para la Mini App ─────────────────────────────

def _contrato_de_la_jornada(session: Session, empleado: Empleado,
                            reserva: ReservaVehiculo) -> tuple[int, str | None] | None:
    """Contrato (id de ``agreement`` y nombre) de la jornada, como lo resuelve el preoperacional.

    Regla (2026-11-26): es **exactamente** el contrato del preoperacional de la reserva (``preoperacional.contrato_id``
    y ``pos.operacional.agreement_id`` apuntan al mismo modelo ``agreement``). Sin preoperacional o sin contrato en
    él, es ``None`` (no se deduce de otras fuentes).
    """
    if not reserva.preop_id:
        return None
    preop = session.get(Preoperacional, reserva.preop_id)
    contrato_id = preop.contrato_odoo_id if preop is not None else None
    if contrato_id is None:
        return None
    extracto = session.get(Extracto, reserva.extracto_id) if reserva.extracto_id else None
    candidatos = (preop_service._contratos_permitidos(extracto, empleado) if extracto is not None
                  else preop_service._contratos_empleado(empleado))
    nombre = next((c.get("nombre") for c in candidatos if c.get("id_contrato") == contrato_id), None)
    if not nombre:
        acuerdo = session.scalar(select(Acuerdo).where(Acuerdo.odoo_id == contrato_id))
        nombre = acuerdo.nombre if acuerdo is not None else None
    return contrato_id, nombre


def form_context(db: Database, empleado: Empleado, telegram_user_id: int,
                 now: datetime | None = None) -> dict[str, Any]:
    """Conductor, contrato, vehículo reservado, KM inicial sugerido y horas por defecto (hora de Colombia).

    ``entrada`` es el inicio de la reserva (``reservado_at``) y ``salida`` es ahora; ambas ``YYYY-MM-DDTHH:MM``.
    ``km_inicial`` es el odómetro del último preoperacional de la jornada (None si no hay).
    Sin reserva vigente lanza ``SinReservaVigenteError`` (409).
    """
    now = now or utc_now()
    with db.session() as session:
        reserva = preop_service.exigir_reserva(session, telegram_user_id)
        v = reserva.vehiculo
        vehiculo = {"id": v.id, "nombre": v.nombre, "matricula": v.matricula,
                    "numero_interno": v.numero_interno, "placa": reservas.placa(v)}
        preop = session.get(Preoperacional, reserva.preop_id) if reserva.preop_id else None
        km_inicial = float(preop.kilometraje) if preop is not None else None
        entrada = _hora_colombia(reserva.reservado_at)
        resuelto = _contrato_de_la_jornada(session, empleado, reserva)
        contrato = {"id": resuelto[0], "nombre": resuelto[1]} if resuelto else None
    return {
        "empleado": {"nombre": empleado.nombre, "puesto_trabajo": empleado.puesto_trabajo,
                     "responsable": empleado.responsable},
        "contrato": contrato,
        "vehiculo": vehiculo,
        "km_inicial": km_inicial,
        "entrada": entrada,
        "salida": _hora_colombia(now),
    }


# ───────────────────────────────────── recepción ──────────────────────────────────────

@dataclass(frozen=True)
class PosRecibido:
    id: int
    uuid: str
    telegram_user_id: int
    sync_status: SyncStatus
    odoo_id: int | None = None
    placa: str | None = None


def resumen_recibido(pos: PosOperacional) -> PosRecibido:
    placa = reservas.placa(pos.vehiculo) if pos.vehiculo is not None else None
    return PosRecibido(pos.id, pos.uuid, pos.telegram_user_id, pos.sync_status, pos.odoo_id, placa)


def nombre_planilla(nombre: str | None, tipo: str) -> str:
    """Nombre del archivo que se envía a Odoo: el original sin ruta y con la extensión de su ``type_file``."""
    base = re.split(r"[\\/]", (nombre or "").strip())[-1]
    base = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", base)
    base = re.sub(r"[^\w .()\-]", "_", base, flags=re.UNICODE).strip(" ._")[:80]
    return f"{base or 'planilla'}.{tipo}"


def submit_diferido(db: Database, identity: TelegramIdentity, data: PosOperacionalIn,
                    tiempos: TiemposDiferido) -> tuple[PosRecibido, bool]:
    """Recibe un envío guardado en el teléfono por falta de señal (regla de ``app/preop/diferido.py``).

    Dentro de la ventana sigue el flujo normal; fuera de ella queda solo como auditoría (con el formulario y la
    planilla) y lanza ``EnvioVencidoError``.
    """
    with db.session() as session:
        existing = session.scalar(select(PosOperacional).where(PosOperacional.uuid == data.uuid))
        if existing is not None:
            if existing.telegram_user_id != identity.user_id:
                raise SubmissionError("Identificador de formulario en uso", status_code=409)
            return resumen_recibido(existing), True
        previo = session.scalar(select(PosOperacionalEnvioDiferido).where(
            PosOperacionalEnvioDiferido.uuid == data.uuid))
        if previo is not None:
            if previo.telegram_user_id != identity.user_id:
                raise SubmissionError("Identificador de formulario en uso", status_code=409)
            raise EnvioVencidoError(previo.motivo or "Envío vencido")

    motivo = motivo_vencido(tiempos.apertura, tiempos.sin_senal_desde, tiempos.recibido)
    if motivo is None:
        return submit(db, identity, data, diferido=tiempos)

    with db.session() as session:
        session.add(PosOperacionalEnvioDiferido(
            uuid=data.uuid, telegram_user_id=identity.user_id, estado=EstadoDiferido.VENCIDO, motivo=motivo,
            apertura_at=tiempos.apertura, sin_senal_desde=tiempos.sin_senal_desde, recibido_at=tiempos.recibido,
            retraso_segundos=tiempos.retraso_segundos, payload_sha256=payload_sha256(data),
            payload=data.model_dump(mode="json", exclude={"diferido"}),
        ))
        try:
            session.flush()
        except IntegrityError as exc:  # el mismo uuid llegó dos veces en paralelo
            raise EnvioVencidoError(motivo) from exc
    logger.warning("Pos-operacional diferido VENCIDO uuid=%s usuario=%s: %s", data.uuid, identity.user_id, motivo)
    raise EnvioVencidoError(motivo)


def submit(db: Database, identity: TelegramIdentity, data: PosOperacionalIn,
           diferido: TiemposDiferido | None = None) -> tuple[PosRecibido, bool]:
    """Guarda el formulario como ``pendiente``. Devuelve (envío, era_duplicado). Idempotente por ``uuid``.

    ``diferido`` (ya validado dentro de la ventana) deja la trazabilidad sin señal.
    """
    with db.session() as session:
        existing = session.scalar(select(PosOperacional).where(PosOperacional.uuid == data.uuid))
        if existing is not None:
            if existing.telegram_user_id != identity.user_id:
                raise SubmissionError("Identificador de formulario en uso", status_code=409)
            return resumen_recibido(existing), True

    try:
        # Odoo solo recibe la planilla en PDF (regla 2026-11-26): una foto se convierte a PDF de una página.
        contenido, _ = soporte_mod.a_pdf(soporte_mod.decodificar(data.planilla))
        tipo = "pdf"
    except ValueError as exc:
        raise SubmissionError(f"Planilla: {exc}") from exc

    with db.session() as session:
        empleado = session.scalar(
            select(Empleado).where(Empleado.codigo_pin == str(identity.user_id), Empleado.activo.is_(True))
            .order_by(Empleado.updated_at.desc()).limit(1)
        )
        if empleado is None:
            raise SubmissionError("Tu usuario de Telegram no está asociado a un empleado activo", 403)
        reserva = preop_service.exigir_reserva(session, identity.user_id, diferido)
        contrato = _contrato_de_la_jornada(session, empleado, reserva)

        pos = PosOperacional(
            uuid=data.uuid,
            telegram_user_id=identity.user_id,
            telegram_username=identity.username,
            firma_nombre=identity.full_name[:255] or empleado.nombre,
            empleado_id=empleado.id,
            vehiculo_id=reserva.vehiculo_id,
            contrato_odoo_id=contrato[0] if contrato else None,
            contrato=contrato[1][:255] if contrato and contrato[1] else None,
            fecha=utc_now(),
            km_inicial=data.km_inicial,
            km_final=data.km_final,
            check_in=data.entrada,
            check_out=data.salida,
            observaciones=data.observaciones,
            planilla=contenido,
            planilla_nombre=nombre_planilla(data.planilla_nombre, tipo),
            planilla_tipo=tipo,
            planilla_tamano=len(contenido),
            planilla_sha256=hashlib.sha256(contenido).hexdigest(),
            sync_status=SyncStatus.PENDIENTE,
            next_attempt_at=utc_now(),
        )
        session.add(pos)
        try:
            session.flush()
            if diferido is not None:
                session.add(PosOperacionalEnvioDiferido(
                    uuid=pos.uuid, telegram_user_id=identity.user_id, estado=EstadoDiferido.ACEPTADO,
                    pos_id=pos.id, apertura_at=diferido.apertura, sin_senal_desde=diferido.sin_senal_desde,
                    recibido_at=diferido.recibido, retraso_segundos=diferido.retraso_segundos,
                    payload_sha256=payload_sha256(data),
                ))
            session.flush()
        except IntegrityError as exc:  # el mismo uuid llegó dos veces en paralelo
            raise SubmissionError("Formulario ya recibido", status_code=409) from exc
        return resumen_recibido(pos), False


# ──────────────────────────────────── envío a Odoo ────────────────────────────────────

def build_odoo_values(pos: PosOperacional) -> dict[str, Any]:
    """Traduce el registro local a ``vals`` del modelo ``pos.operacional``.

    Se omiten ``agreement_id`` y ``vehicle_id`` si no hay (campo vacío en Odoo). No se envían ``lunch_hour`` ni
    ``state`` (los pone Odoo) ni las secciones de gastos y combustible (tienen sus propios formularios).
    ``check_in`` / ``check_out`` van en UTC naive ``YYYY-MM-DD HH:MM:SS``.
    """
    vals: dict[str, Any] = {"employee_id": pos.empleado.odoo_id}
    if pos.contrato_odoo_id:
        vals["agreement_id"] = pos.contrato_odoo_id
    if pos.vehiculo is not None:
        vals["vehicle_id"] = pos.vehiculo.odoo_id
    vals.update({
        "km_start": float(pos.km_inicial),
        "km_end": float(pos.km_final),
        "check_in": _odoo_datetime(pos.check_in),
        "check_out": _odoo_datetime(pos.check_out),
        "attachment": base64.b64encode(pos.planilla).decode("ascii"),  # Binary: base64 sin prefijo data:
        "attachment_filename": pos.planilla_nombre,
        "type_file": pos.planilla_tipo,
        "observations": pos.observaciones or "",
    })
    return vals


@dataclass(frozen=True)
class PosOperacionalOutcome:
    pos_id: int
    telegram_user_id: int
    status: SyncStatus
    odoo_id: int | None = None
    odoo_name: str | None = None
    placa: str | None = None
    error: str | None = None


def _buscar_previo(writer: OdooWriter, vals: dict[str, Any], pos_id: int) -> int | None:
    """Dedupe por empleado + entrada + salida. Un fallo permanente de la búsqueda no impide crear (solo se avisa)."""
    try:
        return writer.buscar_pos_operacional(vals)
    except OdooPermanentError as exc:
        logger.warning("Pos-operacional %s: no se pudo buscar un duplicado en Odoo (%s); se crea igual", pos_id, exc)
        return None


def send_one(db: Database, writer: OdooWriter, pos_id: int, max_attempts: int) -> PosOperacionalOutcome:
    """Envía un pos-operacional ya reclamado (``enviando``) y deja el estado final en la BD.

    Antes de crear busca uno igual en Odoo (empleado + entrada + salida): un reintento tras un timeout no duplica.
    Un rechazo de Odoo (Fault de validación) deja el registro ``fallido`` sin reintento; un error de red o de
    concurrencia queda en ``error`` y se reintenta con backoff hasta ``max_attempts``.
    """
    with db.session() as session:
        pos = session.get(PosOperacional, pos_id)
        if pos is None or pos.sync_status != SyncStatus.ENVIANDO:
            raise LookupError(f"pos-operacional {pos_id} no está en estado enviando")
        vals = build_odoo_values(pos)
        ref, submitted_at, attempts, user_id = pos.uuid, pos.fecha, pos.attempts, pos.telegram_user_id
        placa = reservas.placa(pos.vehiculo) if pos.vehiculo is not None else None

    status: SyncStatus
    error: str | None = None
    odoo_id: int | None = None
    odoo_name: str | None = None
    try:
        odoo_id = _buscar_previo(writer, vals, pos_id)
        if odoo_id is not None:
            logger.info("Pos-operacional %s ya existía en Odoo (id=%s): no se duplicó", pos_id, odoo_id)
        else:
            odoo_id, odoo_name, _ = writer.create_pos_operacional(ref, vals, submitted_at, attempts > 1)
        status = SyncStatus.ENVIADO
    except OdooPermanentError as exc:
        status, error = SyncStatus.FALLIDO, f"Odoo rechazó el registro: {exc}"
    except OdooTransientError as exc:
        status = SyncStatus.FALLIDO if attempts >= max_attempts else SyncStatus.ERROR
        error = f"Intento {attempts}/{max_attempts}: {exc}"

    now = utc_now()
    with db.session() as session:
        pos = session.get(PosOperacional, pos_id)
        assert pos is not None
        pos.sync_status = status
        pos.error_log = error
        if status == SyncStatus.ENVIADO:
            pos.odoo_id, pos.odoo_name, pos.sent_at = odoo_id, odoo_name, now
            pos.next_attempt_at = None
        elif status == SyncStatus.ERROR:
            pos.next_attempt_at = now + preop_service.backoff(attempts)
        else:
            pos.next_attempt_at = None
    log = logger.info if status == SyncStatus.ENVIADO else logger.warning
    log("Pos-operacional %s -> %s%s", pos_id, status.value, f" ({error})" if error else f" odoo_id={odoo_id}")
    return PosOperacionalOutcome(pos_id, user_id, status, odoo_id, odoo_name, placa, error)


def claim_batch(db: Database, limit: int, lease_seconds: int, now: datetime | None = None) -> list[int]:
    return preop_service.claim_batch(db, limit, lease_seconds, now, model=PosOperacional)


def requeue_failed(db: Database, ids: list[int] | None = None) -> int:
    return preop_service.requeue_failed(db, ids, model=PosOperacional)
