"""Lógica de negocio del preoperacional: recepción, cola de envío y consultas de la Mini App.

Todo es síncrono (SQLAlchemy + XML-RPC); el código async lo llama con
``asyncio.to_thread`` para no bloquear el event loop de FastAPI/Telegram.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .checklist import count_no_conformes
from .db import Database
from .models import Empleado, Extracto, Flota, Preoperacional, SyncStatus, utc_now
from .odoo_writer import OdooPermanentError, OdooTransientError, OdooWriter, build_odoo_values
from .schemas import PreoperacionalIn

logger = logging.getLogger(__name__)

BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 3600


class SubmissionError(Exception):
    """El formulario no puede aceptarse (mensaje apto para mostrar al usuario)."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class TelegramIdentity:
    user_id: int
    username: str | None
    full_name: str


# ───────────────────────────── consultas para la Mini App ─────────────────────────────

def employee_for_telegram(db: Database, telegram_user_id: int) -> Empleado | None:
    """Empleado activo cuyo PIN (hr.employee.pin) es el id de Telegram."""
    with db.session() as session:
        rows = session.scalars(
            select(Empleado).where(Empleado.codigo_pin == str(telegram_user_id), Empleado.activo.is_(True))
            .order_by(Empleado.updated_at.desc()).limit(2)
        ).all()
    if len(rows) > 1:
        logger.warning("PIN %s asignado a más de un empleado activo; se usa el más reciente", telegram_user_id)
    return rows[0] if rows else None


def form_context(db: Database, empleado: Empleado, now: datetime | None = None) -> dict[str, Any]:
    """Datos de autocompletado: empleado, vehículos activos de su compañía y extractos vigentes."""
    now = now or utc_now()
    with db.session() as session:
        vehiculos = session.scalars(
            select(Flota).where(Flota.activo.is_(True), Flota.company_odoo_id == empleado.company_odoo_id)
            .order_by(Flota.numero_interno, Flota.nombre)
        ).all()
        # Vigente: abarca "ahora" con 1 día de margen a cada lado (turnos que cruzan medianoche).
        margin = timedelta(days=1)
        extractos = session.scalars(
            select(Extracto).where(
                Extracto.activo.is_(True),
                Extracto.company_odoo_id == empleado.company_odoo_id,
                Extracto.vehiculo_id.is_not(None),
                or_(Extracto.fecha_hora_inicio.is_(None), Extracto.fecha_hora_inicio <= now + margin),
                or_(Extracto.fecha_hora_fin.is_(None), Extracto.fecha_hora_fin >= now - margin),
            ).order_by(Extracto.fecha_hora_inicio.desc())
        ).unique().all()
    contratos_empleado = [
        {"id_contrato": cid, "nombre": nombre}
        for cid, nombre in ((empleado.id_contract, empleado.contrato),
                            (empleado.contrato_temporal_id, empleado.contrato_temporal))
        if cid
    ]
    return {
        "empleado": {
            "nombre": empleado.nombre, "puesto_trabajo": empleado.puesto_trabajo,
            "responsable": empleado.responsable, "contratos": contratos_empleado,
        },
        "vehiculos": [
            {"id": v.id, "odoo_id": v.odoo_id, "nombre": v.nombre, "matricula": v.matricula,
             "numero_interno": v.numero_interno, "odometer_post": v.odometer_post}
            for v in vehiculos
        ],
        "extractos": [
            {"id": e.id, "codigo": e.codigo, "vehiculo_odoo_id": e.vehiculo_id, "estado": e.estado,
             "conductor": e.conductor, "contratos": e.contratos_list,
             "fecha_hora_inicio": e.fecha_hora_inicio.isoformat() if e.fecha_hora_inicio else None,
             "fecha_hora_fin": e.fecha_hora_fin.isoformat() if e.fecha_hora_fin else None}
            for e in extractos
        ],
    }


# ───────────────────────────────────── recepción ──────────────────────────────────────

def submit(db: Database, identity: TelegramIdentity, data: PreoperacionalIn) -> tuple[Preoperacional, bool]:
    """Guarda el formulario como ``pendiente``. Devuelve (registro, era_duplicado).

    Idempotente por ``uuid``: un reenvío (doble toque, red inestable) devuelve el original.
    """
    with db.session() as session:
        existing = session.scalar(select(Preoperacional).where(Preoperacional.uuid == data.uuid))
        if existing is not None:
            if existing.telegram_user_id != identity.user_id:
                raise SubmissionError("Identificador de formulario en uso", status_code=409)
            return existing, True

        empleado = session.scalar(
            select(Empleado).where(Empleado.codigo_pin == str(identity.user_id), Empleado.activo.is_(True))
            .order_by(Empleado.updated_at.desc()).limit(1)
        )
        if empleado is None:
            raise SubmissionError("Tu usuario de Telegram no está asociado a un empleado activo", 403)

        vehiculo = session.get(Flota, data.vehiculo_id)
        if vehiculo is None or not vehiculo.activo:
            raise SubmissionError("Vehículo no encontrado o inactivo")
        if vehiculo.company_odoo_id != empleado.company_odoo_id:
            raise SubmissionError("El vehículo no pertenece a tu compañía", 403)

        extracto = None
        if data.extracto_id is not None:
            extracto = session.get(Extracto, data.extracto_id)
            if extracto is None or not extracto.activo:
                raise SubmissionError("Extracto no encontrado o inactivo")
            if extracto.vehiculo_id is not None and extracto.vehiculo_id != vehiculo.odoo_id:
                raise SubmissionError("El extracto no corresponde al vehículo seleccionado")

        contrato = data.contrato_odoo_id
        allowed_contracts = {c["id_contrato"] for c in (extracto.contratos_list if extracto else [])}
        allowed_contracts |= {cid for cid in (empleado.id_contract, empleado.contrato_temporal_id) if cid}
        if contrato is not None and contrato not in allowed_contracts:
            raise SubmissionError("El contrato no corresponde al extracto ni al empleado")
        if contrato is None and extracto is not None and len(extracto.contratos_list) == 1:
            contrato = extracto.contratos_list[0]["id_contrato"]

        preop = Preoperacional(
            uuid=data.uuid,
            telegram_user_id=identity.user_id,
            telegram_username=identity.username,
            firma_nombre=identity.full_name[:255] or empleado.nombre,
            empleado_id=empleado.id,
            vehiculo_id=vehiculo.id,
            extracto_id=extracto.id if extracto else None,
            contrato_odoo_id=contrato,
            fecha=utc_now(),
            kilometraje=data.kilometraje,
            numero_interno=vehiculo.numero_interno,
            observaciones=data.observaciones,
            checklist=dict(data.checklist),
            llantas=dict(data.llantas),
            extra=dict(data.extra),
            no_conformes=count_no_conformes(data.checklist, data.llantas),
            sync_status=SyncStatus.PENDIENTE,
            next_attempt_at=utc_now(),
        )
        session.add(preop)
        try:
            session.flush()
        except IntegrityError as exc:
            # Carrera: el mismo uuid llegó dos veces en paralelo.
            raise SubmissionError("Formulario ya recibido", status_code=409) from exc
        return preop, False


# ──────────────────────────────────── cola de envío ───────────────────────────────────

def backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(BACKOFF_BASE_SECONDS * 2 ** max(attempts - 1, 0), BACKOFF_MAX_SECONDS))


def claim_batch(db: Database, limit: int, lease_seconds: int, now: datetime | None = None) -> list[int]:
    """Toma registros listos para enviar y los marca ``enviando`` (lease).

    En PostgreSQL usa ``FOR UPDATE SKIP LOCKED``: varios workers no toman el mismo registro.
    """
    now = now or utc_now()
    stale = now - timedelta(seconds=lease_seconds)
    ready = or_(
        Preoperacional.sync_status == SyncStatus.PENDIENTE,
        and_(Preoperacional.sync_status == SyncStatus.ERROR, Preoperacional.next_attempt_at <= now),
        and_(Preoperacional.sync_status == SyncStatus.ENVIANDO, Preoperacional.last_attempt_at < stale),
    )
    with db.session() as session:
        ids = list(session.scalars(
            select(Preoperacional.id).where(ready).order_by(Preoperacional.created_at).limit(limit)
            .with_for_update(skip_locked=True)
        ))
        if ids:
            session.execute(
                update(Preoperacional).where(Preoperacional.id.in_(ids))
                .values(sync_status=SyncStatus.ENVIANDO, last_attempt_at=now,
                        attempts=Preoperacional.attempts + 1)
                .execution_options(synchronize_session=False)
            )
    return ids


@dataclass(frozen=True)
class SendOutcome:
    preop_id: int
    telegram_user_id: int
    status: SyncStatus
    odoo_name: str | None = None
    no_conformes: int = 0
    error: str | None = None


def _boss_odoo_id(session: Session, empleado: Empleado) -> int | None:
    """Jefe inmediato: la API entrega solo el nombre del responsable; se resuelve si es único."""
    if not empleado.responsable:
        return None
    ids = session.scalars(
        select(Empleado.odoo_id).where(Empleado.nombre == empleado.responsable, Empleado.activo.is_(True))
        .limit(2)
    ).all()
    return ids[0] if len(ids) == 1 else None


def send_one(db: Database, writer: OdooWriter, preop_id: int, max_attempts: int) -> SendOutcome:
    """Envía un registro ya reclamado (``enviando``) y deja el estado final en la BD."""
    with db.session() as session:
        preop = session.get(Preoperacional, preop_id)
        if preop is None or preop.sync_status != SyncStatus.ENVIANDO:
            raise LookupError(f"preoperacional {preop_id} no está en estado enviando")
        vals = build_odoo_values(preop, _boss_odoo_id(session, preop.empleado))
        ref, submitted_at, attempts = preop.uuid, preop.fecha, preop.attempts
        user_id, no_conformes = preop.telegram_user_id, preop.no_conformes

    status: SyncStatus
    error: str | None = None
    odoo_id: int | None = None
    odoo_name: str | None = None
    try:
        # Red fuera de la transacción: no se retiene una conexión de BD durante el XML-RPC.
        odoo_id, odoo_name, reused = writer.create_preoperacional(ref, vals, submitted_at, attempts > 1)
        status = SyncStatus.ENVIADO
        if reused:
            logger.info("Preoperacional %s ya existía en Odoo (id=%s): no se duplicó", preop_id, odoo_id)
    except OdooPermanentError as exc:
        status, error = SyncStatus.FALLIDO, f"Odoo rechazó el registro: {exc}"
    except OdooTransientError as exc:
        status = SyncStatus.FALLIDO if attempts >= max_attempts else SyncStatus.ERROR
        error = f"Intento {attempts}/{max_attempts}: {exc}"

    now = utc_now()
    with db.session() as session:
        preop = session.get(Preoperacional, preop_id)
        assert preop is not None
        preop.sync_status = status
        preop.error_log = error
        if status == SyncStatus.ENVIADO:
            preop.odoo_id, preop.odoo_name, preop.sent_at = odoo_id, odoo_name, now
            preop.next_attempt_at = None
        elif status == SyncStatus.ERROR:
            preop.next_attempt_at = now + backoff(attempts)
        else:
            preop.next_attempt_at = None
    log = logger.info if status == SyncStatus.ENVIADO else logger.warning
    log("Preoperacional %s -> %s%s", preop_id, status.value, f" ({error})" if error else f" odoo_id={odoo_id}")
    return SendOutcome(preop_id, user_id, status, odoo_name, no_conformes, error)


def requeue_failed(db: Database, preop_ids: list[int] | None = None) -> int:
    """Devuelve a ``pendiente`` registros ``fallido`` (tras corregir la causa). None = todos."""
    stmt = update(Preoperacional).where(Preoperacional.sync_status == SyncStatus.FALLIDO)
    if preop_ids:
        stmt = stmt.where(Preoperacional.id.in_(preop_ids))
    with db.session() as session:
        result = session.execute(
            # attempts=1: el siguiente envío se trata como "resultado previo incierto" y busca
            # también por llave natural antes de crear (un fallido por timeout pudo crearse).
            stmt.values(sync_status=SyncStatus.PENDIENTE, attempts=1, next_attempt_at=utc_now(), error_log=None)
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]  # CursorResult
