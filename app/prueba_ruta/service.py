"""Lógica de la Prueba de Ruta: contexto, recepción (incluido el envío sin señal) y envío a Odoo.

Orden de las reglas de acceso (igual en el contexto de la Mini App y al recibir):

1. El id de Telegram es el PIN de un empleado activo (si no, 403).
2. Tiene un extracto **aprobado y vigente** con un acuerdo autorizado
   (``acuerdos.contrato_autorizado``); si ya reservó el vehículo, debe serlo el extracto de esa
   reserva. Si no, **"Contrato No Autorizado"** (403).
3. Tiene una reserva vigente (preoperacional de la jornada; ``reservas.reserva_vigente``); si no,
   ``SinReservaVigenteError`` (409). Esta regla se evalúa **antes** que la del contrato.

Todo es síncrono (SQLAlchemy + XML-RPC); el código async lo llama con ``asyncio.to_thread``.
"""
from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..preop import reservas
from ..preop import service as preop_service
from ..preop.db import Database
from ..preop.diferido import motivo_vencido
from ..preop.models import (
    Empleado,
    EstadoDiferido,
    PruebaRuta,
    PruebaRutaEnvioDiferido,
    PruebaRutaFoto,
    SyncStatus,
    utc_now,
)
from ..preop.odoo_writer import OdooPermanentError, OdooTransientError, OdooWriter
from ..preop.schemas import decode_photo
from ..preop.service import (
    EnvioVencidoError,
    SubmissionError,
    TelegramIdentity,
    TiemposDiferido,
    payload_sha256,
)
from .acuerdos import CONTRATO_NO_AUTORIZADO, contrato_autorizado, contratos_extractos
from .catalogo import FOTOS, PREGUNTAS_BY_KEY, con_novedad
from .schemas import PruebaRutaIn

logger = logging.getLogger(__name__)

# ───────────────────────────── consultas para la Mini App ─────────────────────────────

def form_context(db: Database, empleado: Empleado, telegram_user_id: int,
                 autorizados: frozenset[str]) -> dict[str, Any]:
    """Empleado, autorización por el extracto aprobado y vehículo reservado por su preoperacional (sin reserva: 409)."""
    with db.session() as session:
        reserva = preop_service.exigir_reserva(session, telegram_user_id)
        extracto_id = reserva.extracto_id
        contrato = contrato_autorizado(session, empleado, autorizados, extracto_id)
        contratos = contratos_extractos(session, empleado, extracto_id)
        vehiculo = None
        if contrato is not None:
            v = reserva.vehiculo
            vehiculo = {"id": v.id, "nombre": v.nombre, "matricula": v.matricula,
                        "numero_interno": v.numero_interno, "placa": reservas.placa(v)}
    return {
        "empleado": {"nombre": empleado.nombre, "puesto_trabajo": empleado.puesto_trabajo,
                     "responsable": empleado.responsable,
                     "contrato": empleado.contrato or empleado.contrato_temporal},
        "autorizado": contrato is not None,
        "mensaje": None if contrato else CONTRATO_NO_AUTORIZADO,
        "contrato": contrato.nombre if contrato else None,
        "extracto": contrato.extracto_codigo if contrato else None,
        # Contratos de sus extractos aprobados y vigentes (los de la reserva, si la hay): para el bloqueo.
        "contratos_extracto": contratos,
        "vehiculo": vehiculo,
    }


# ───────────────────────────────────── recepción ──────────────────────────────────────

def submit_diferido(db: Database, identity: TelegramIdentity, data: PruebaRutaIn, tiempos: TiemposDiferido,
                    autorizados: frozenset[str]) -> tuple[PruebaRuta, bool]:
    """Recibe una prueba guardada en el teléfono por falta de señal (regla de ``app/preop/diferido.py``).

    Dentro de la ventana sigue el flujo normal; fuera de ella queda solo como auditoría
    (con el formulario y las fotos) y lanza ``EnvioVencidoError``.
    """
    with db.session() as session:
        existing = session.scalar(select(PruebaRuta).where(PruebaRuta.uuid == data.uuid))
        if existing is not None:
            if existing.telegram_user_id != identity.user_id:
                raise SubmissionError("Identificador de formulario en uso", status_code=409)
            return existing, True
        previo = session.scalar(select(PruebaRutaEnvioDiferido).where(PruebaRutaEnvioDiferido.uuid == data.uuid))
        if previo is not None:
            if previo.telegram_user_id != identity.user_id:
                raise SubmissionError("Identificador de formulario en uso", status_code=409)
            raise EnvioVencidoError(previo.motivo or "Envío vencido")

    motivo = motivo_vencido(tiempos.apertura, tiempos.sin_senal_desde, tiempos.recibido)
    if motivo is None:
        return submit(db, identity, data, autorizados, diferido=tiempos)

    with db.session() as session:
        session.add(PruebaRutaEnvioDiferido(
            uuid=data.uuid, telegram_user_id=identity.user_id, estado=EstadoDiferido.VENCIDO, motivo=motivo,
            apertura_at=tiempos.apertura, sin_senal_desde=tiempos.sin_senal_desde, recibido_at=tiempos.recibido,
            retraso_segundos=tiempos.retraso_segundos, payload_sha256=payload_sha256(data),
            payload=data.model_dump(mode="json", exclude={"diferido", "km1", "km2"}),
            foto_km1=decode_photo(data.km1)[0], foto_km2=decode_photo(data.km2)[0],
        ))
        try:
            session.flush()
        except IntegrityError as exc:  # el mismo uuid llegó dos veces en paralelo
            raise EnvioVencidoError(motivo) from exc
    logger.warning("Prueba de ruta diferida VENCIDA uuid=%s usuario=%s: %s", data.uuid, identity.user_id, motivo)
    raise EnvioVencidoError(motivo)


def submit(db: Database, identity: TelegramIdentity, data: PruebaRutaIn, autorizados: frozenset[str],
           diferido: TiemposDiferido | None = None) -> tuple[PruebaRuta, bool]:
    """Guarda la prueba como ``pendiente`` con sus fotos. Devuelve (registro, era_duplicado).

    Idempotente por ``uuid``. ``diferido`` (ya validado dentro de la ventana) deja la
    trazabilidad del envío sin señal.
    """
    with db.session() as session:
        existing = session.scalar(select(PruebaRuta).where(PruebaRuta.uuid == data.uuid))
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
        reserva = preop_service.exigir_reserva(session, identity.user_id, diferido)
        contrato = contrato_autorizado(session, empleado, autorizados, reserva.extracto_id)
        if contrato is None:
            raise SubmissionError(CONTRATO_NO_AUTORIZADO, 403)
        if data.vehiculo_id is not None and data.vehiculo_id != reserva.vehiculo_id:
            raise SubmissionError("El vehículo no es el reservado por tu preoperacional", 409)

        respuestas = dict(data.respuestas)
        prueba = PruebaRuta(
            uuid=data.uuid,
            telegram_user_id=identity.user_id,
            telegram_username=identity.username,
            firma_nombre=identity.full_name[:255] or empleado.nombre,
            empleado_id=empleado.id,
            vehiculo_id=reserva.vehiculo_id,
            preop_id=reserva.preop_id,
            contrato_odoo_id=contrato.odoo_id,
            contrato=contrato.nombre[:255] if contrato.nombre else None,
            fecha=utc_now(),
            respuestas=respuestas,
            observaciones=data.observaciones,
            no_conformes=len(con_novedad(respuestas)),
            sync_status=SyncStatus.PENDIENTE,
            next_attempt_at=utc_now(),
        )
        session.add(prueba)
        try:
            session.flush()
            for tipo, value in ((t, getattr(data, t)) for t in FOTOS):
                foto, mime = decode_photo(value)
                session.add(PruebaRutaFoto(prueba_id=prueba.id, tipo=tipo, mime=mime, contenido=foto,
                                           tamano=len(foto), sha256=hashlib.sha256(foto).hexdigest()))
            if diferido is not None:
                session.add(PruebaRutaEnvioDiferido(
                    uuid=prueba.uuid, telegram_user_id=identity.user_id, estado=EstadoDiferido.ACEPTADO,
                    prueba_id=prueba.id, apertura_at=diferido.apertura, sin_senal_desde=diferido.sin_senal_desde,
                    recibido_at=diferido.recibido, retraso_segundos=diferido.retraso_segundos,
                    payload_sha256=payload_sha256(data),
                ))
            session.flush()
        except IntegrityError as exc:  # el mismo uuid llegó dos veces en paralelo
            raise SubmissionError("Formulario ya recibido", status_code=409) from exc
        return prueba, False


# ──────────────────────────────────── envío a Odoo ────────────────────────────────────

def build_odoo_values(prueba: PruebaRuta, fotos: dict[str, bytes], boss_odoo_id: int | None = None) -> dict[str, Any]:
    """Traduce el registro local a ``vals`` del modelo ``prueba.ruta``.

    ``state`` y ``plan_accion`` los calcula Odoo (cualquier "Sí" -> no conforme). El modelo
    no tiene campo de contrato: el acuerdo autorizado queda solo en la BD del bot.
    """
    vals: dict[str, Any] = {
        "user_id": prueba.empleado.odoo_id,
        "vehicle_id": prueba.vehiculo.odoo_id,
        "company_id": prueba.empleado.company_odoo_id,
        "observaciones": prueba.observaciones or "",
    }
    if boss_odoo_id:
        vals["jefe_inmediato"] = boss_odoo_id
    vals.update({key: value for key, value in prueba.respuestas.items() if key in PREGUNTAS_BY_KEY})
    for tipo, contenido in fotos.items():
        if tipo in FOTOS:
            vals[tipo] = base64.b64encode(contenido).decode("ascii")  # Binary: base64 sin prefijo data:
    return vals


@dataclass(frozen=True)
class AvisoJefePrueba:
    telegram_id: int
    conductor: str
    vehiculo: str
    novedades: tuple[str, ...]


@dataclass(frozen=True)
class PruebaRutaOutcome:
    prueba_id: int
    telegram_user_id: int
    status: SyncStatus
    odoo_name: str | None = None
    no_conformes: int = 0
    error: str | None = None
    aviso_jefe: AvisoJefePrueba | None = None


def _aviso_jefe(session: Session, prueba: PruebaRuta) -> AvisoJefePrueba | None:
    novedades = con_novedad(prueba.respuestas)
    if not novedades:
        return None
    jefe = preop_service.jefe_inmediato(session, prueba.empleado)
    pin = jefe.codigo_pin if jefe else None
    if not pin or not pin.isdigit():
        logger.warning("Prueba de ruta %s con novedades: el jefe (%s) no tiene id de Telegram en el PIN",
                       prueba.id, prueba.empleado.responsable or "sin responsable")
        return None
    v = prueba.vehiculo
    return AvisoJefePrueba(int(pin), prueba.empleado.nombre, " · ".join(x for x in (v.matricula, v.nombre) if x),
                           tuple(p.corto for p in novedades))


def send_one(db: Database, writer: OdooWriter, prueba_id: int, max_attempts: int) -> PruebaRutaOutcome:
    """Envía una prueba ya reclamada (``enviando``) y deja el estado final en la BD."""
    with db.session() as session:
        prueba = session.get(PruebaRuta, prueba_id)
        if prueba is None or prueba.sync_status != SyncStatus.ENVIANDO:
            raise LookupError(f"prueba de ruta {prueba_id} no está en estado enviando")
        fotos = {f.tipo: f.contenido for f in session.scalars(
            select(PruebaRutaFoto).where(PruebaRutaFoto.prueba_id == prueba.id))}
        jefe = preop_service.jefe_inmediato(session, prueba.empleado)
        vals = build_odoo_values(prueba, fotos, jefe.odoo_id if jefe else None)
        ref, submitted_at, attempts = prueba.uuid, prueba.fecha, prueba.attempts
        aviso = _aviso_jefe(session, prueba)
        user_id, no_conformes = prueba.telegram_user_id, prueba.no_conformes

    status: SyncStatus
    error: str | None = None
    odoo_id: int | None = None
    odoo_name: str | None = None
    try:
        odoo_id, odoo_name, reused = writer.create_prueba_ruta(ref, vals, submitted_at, attempts > 1)
        status = SyncStatus.ENVIADO
        if reused:
            logger.info("Prueba de ruta %s ya existía en Odoo (id=%s): no se duplicó", prueba_id, odoo_id)
    except OdooPermanentError as exc:
        status, error = SyncStatus.FALLIDO, f"Odoo rechazó el registro: {exc}"
    except OdooTransientError as exc:
        status = SyncStatus.FALLIDO if attempts >= max_attempts else SyncStatus.ERROR
        error = f"Intento {attempts}/{max_attempts}: {exc}"

    now = utc_now()
    with db.session() as session:
        prueba = session.get(PruebaRuta, prueba_id)
        assert prueba is not None
        prueba.sync_status = status
        prueba.error_log = error
        if status == SyncStatus.ENVIADO:
            prueba.odoo_id, prueba.odoo_name, prueba.sent_at = odoo_id, odoo_name, now
            prueba.next_attempt_at = None
        elif status == SyncStatus.ERROR:
            prueba.next_attempt_at = now + preop_service.backoff(attempts)
        else:
            prueba.next_attempt_at = None
    log = logger.info if status == SyncStatus.ENVIADO else logger.warning
    log("Prueba de ruta %s -> %s%s", prueba_id, status.value, f" ({error})" if error else f" odoo_id={odoo_id}")
    return PruebaRutaOutcome(prueba_id, user_id, status, odoo_name, no_conformes, error,
                             aviso if status == SyncStatus.ENVIADO else None)


def claim_batch(db: Database, limit: int, lease_seconds: int, now: datetime | None = None) -> list[int]:
    return preop_service.claim_batch(db, limit, lease_seconds, now, model=PruebaRuta)


def requeue_failed(db: Database, ids: list[int] | None = None) -> int:
    return preop_service.requeue_failed(db, ids, model=PruebaRuta)
