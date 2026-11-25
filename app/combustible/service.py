"""Lógica del registro de combustible: contexto, recepción (incluido el envío sin señal) y envío a Odoo.

Reglas de acceso (igual en el contexto de la Mini App y al recibir):

1. El id de Telegram es el PIN de un empleado activo (si no, 403).
2. Tiene un vehículo reservado por su preoperacional de la jornada (``reservas_vehiculo``);
   si no, debe hacer primero el preoperacional (409). El contrato (``agreement_id``) es el de
   ese preoperacional, que salió de su extracto aprobado.
3. El kilometraje del tanqueo es **mayor o igual** al último registrado (Odoo, la flota
   sincronizada y el odómetro del preoperacional de la jornada).

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
    Acuerdo,
    Combustible,
    CombustibleEnvioDiferido,
    CombustibleFactura,
    Empleado,
    EstadoDiferido,
    Extracto,
    Preoperacional,
    ProveedorCombustible,
    ReservaVehiculo,
    SyncStatus,
    utc_now,
)
from ..preop.odoo_writer import OdooPermanentError, OdooTransientError, OdooWriter
from ..preop.service import (
    EnvioVencidoError,
    OdometroReader,
    SubmissionError,
    TelegramIdentity,
    TiemposDiferido,
    payload_sha256,
    ultimo_odometro,
)
from ..prueba_ruta.acuerdos import _PREFIJO_CODIGO
from .catalogo import TIPOS_BY_KEY, form_definition
from .proveedores import listar_activos
from .schemas import CombustibleIn, decode_pdf

logger = logging.getLogger(__name__)

MSG_SIN_PREOP = ("No tienes un vehículo reservado. Diligencia primero el ✅ Preoperacional del vehículo "
                 "y luego registra el combustible.")


def _reserva_propia(session: Session, telegram_user_id: int) -> ReservaVehiculo | None:
    """Vehículo reservado a este conductor por su preoperacional (libera antes las vencidas)."""
    reservas.liberar_vencidas(session)
    return session.scalar(select(ReservaVehiculo).where(ReservaVehiculo.telegram_user_id == telegram_user_id))


def _contrato(session: Session, reserva: ReservaVehiculo) -> tuple[int | None, str | None]:
    """Contrato (agreement) del preoperacional que reservó el vehículo: (id de Odoo, nombre)."""
    preop = session.get(Preoperacional, reserva.preop_id) if reserva.preop_id else None
    cid = preop.contrato_odoo_id if preop else None
    if not cid:
        return None, None
    acuerdo = session.scalar(select(Acuerdo).where(Acuerdo.odoo_id == cid))
    if acuerdo is not None:
        return cid, _PREFIJO_CODIGO.sub("", acuerdo.nombre)
    extracto = session.get(Extracto, reserva.extracto_id) if reserva.extracto_id else None
    nombre = next((c.get("nombre") for c in (extracto.contratos_list if extracto else [])
                   if c.get("id_contrato") == cid), None)
    return cid, nombre


def _km_minimo(session: Session, reserva: ReservaVehiculo, odometro: OdometroReader | None) -> float | None:
    """Último kilometraje conocido: Odoo (o la flota sincronizada) y el del preoperacional de la jornada."""
    valores = [ultimo_odometro(reserva.vehiculo, odometro)]
    preop = session.get(Preoperacional, reserva.preop_id) if reserva.preop_id else None
    if preop is not None:
        valores.append(preop.kilometraje)
    conocidos = [float(v) for v in valores if v is not None]
    return max(conocidos) if conocidos else None


# ───────────────────────────── consultas para la Mini App ─────────────────────────────

def form_full(db: Database) -> dict[str, Any]:
    """Catálogo del formulario más los proveedores aprobados activos."""
    with db.session() as session:
        proveedores = [{"id": p.id, "nombre": p.nombre} for p in listar_activos(session)]
    return form_definition() | {"proveedores": proveedores}


def form_context(db: Database, empleado: Empleado, telegram_user_id: int,
                 odometro: OdometroReader | None = None) -> dict[str, Any]:
    """Comprador, vehículo reservado por su preoperacional (o None), contrato y último kilometraje."""
    with db.session() as session:
        reserva = _reserva_propia(session, telegram_user_id)
        vehiculo = contrato = km = None
        if reserva is not None:
            v = reserva.vehiculo
            vehiculo = {"id": v.id, "nombre": v.nombre, "matricula": v.matricula,
                        "numero_interno": v.numero_interno, "placa": reservas.placa(v)}
            contrato = _contrato(session, reserva)[1]
            km = _km_minimo(session, reserva, odometro)
    return {
        "empleado": {"nombre": empleado.nombre, "puesto_trabajo": empleado.puesto_trabajo,
                     "responsable": empleado.responsable},
        "contrato": contrato,
        "vehiculo": vehiculo,
        "ultimo_km": km,
        "hoy": reservas.fecha_jornada().isoformat(),
    }


# ───────────────────────────────────── recepción ──────────────────────────────────────

def submit_diferido(db: Database, identity: TelegramIdentity, data: CombustibleIn, tiempos: TiemposDiferido,
                    odometro: OdometroReader | None = None) -> tuple[Combustible, bool]:
    """Recibe un tanqueo guardado en el teléfono por falta de señal (regla de ``app/preop/diferido.py``).

    Dentro de la ventana sigue el flujo normal; fuera de ella queda solo como auditoría
    (con el formulario y la factura) y lanza ``EnvioVencidoError``.
    """
    with db.session() as session:
        existing = session.scalar(select(Combustible).where(Combustible.uuid == data.uuid))
        if existing is not None:
            if existing.telegram_user_id != identity.user_id:
                raise SubmissionError("Identificador de formulario en uso", status_code=409)
            return existing, True
        previo = session.scalar(select(CombustibleEnvioDiferido).where(CombustibleEnvioDiferido.uuid == data.uuid))
        if previo is not None:
            if previo.telegram_user_id != identity.user_id:
                raise SubmissionError("Identificador de formulario en uso", status_code=409)
            raise EnvioVencidoError(previo.motivo or "Envío vencido")

    motivo = motivo_vencido(tiempos.apertura, tiempos.sin_senal_desde, tiempos.recibido)
    if motivo is None:
        return submit(db, identity, data, odometro, diferido=tiempos)

    with db.session() as session:
        session.add(CombustibleEnvioDiferido(
            uuid=data.uuid, telegram_user_id=identity.user_id, estado=EstadoDiferido.VENCIDO, motivo=motivo,
            apertura_at=tiempos.apertura, sin_senal_desde=tiempos.sin_senal_desde, recibido_at=tiempos.recibido,
            retraso_segundos=tiempos.retraso_segundos, payload_sha256=payload_sha256(data),
            payload=data.model_dump(mode="json", exclude={"diferido", "factura"}),
            factura=decode_pdf(data.factura) if data.factura else None,
        ))
        try:
            session.flush()
        except IntegrityError as exc:  # el mismo uuid llegó dos veces en paralelo
            raise EnvioVencidoError(motivo) from exc
    logger.warning("Combustible diferido VENCIDO uuid=%s usuario=%s: %s", data.uuid, identity.user_id, motivo)
    raise EnvioVencidoError(motivo)


def submit(db: Database, identity: TelegramIdentity, data: CombustibleIn, odometro: OdometroReader | None = None,
           diferido: TiemposDiferido | None = None) -> tuple[Combustible, bool]:
    """Guarda el tanqueo como ``pendiente`` con su factura. Devuelve (registro, era_duplicado).

    Idempotente por ``uuid``. ``diferido`` (ya validado dentro de la ventana) deja la
    trazabilidad del envío sin señal.
    """
    with db.session() as session:
        existing = session.scalar(select(Combustible).where(Combustible.uuid == data.uuid))
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
        reserva = _reserva_propia(session, identity.user_id)
        if reserva is None:
            raise SubmissionError(MSG_SIN_PREOP, 409)
        if data.vehiculo_id is not None and data.vehiculo_id != reserva.vehiculo_id:
            raise SubmissionError("El vehículo no es el reservado por tu preoperacional", 409)

        proveedor = None
        if data.proveedor_id is not None:
            proveedor = session.get(ProveedorCombustible, data.proveedor_id)
            if proveedor is None or not proveedor.activo:
                raise SubmissionError("El proveedor ya no está en la lista de aprobados; elígelo de nuevo o escríbelo")

        minimo = _km_minimo(session, reserva, odometro)
        if minimo is not None and data.kilometraje < minimo:
            raise SubmissionError(
                f"El kilometraje ({data.kilometraje:,.0f} km) debe ser mayor o igual al último registrado "
                f"({minimo:,.0f} km)".replace(",", "."))

        contrato_id, _ = _contrato(session, reserva)
        registro = Combustible(
            uuid=data.uuid,
            telegram_user_id=identity.user_id,
            telegram_username=identity.username,
            firma_nombre=identity.full_name[:255] or empleado.nombre,
            empleado_id=empleado.id,
            vehiculo_id=reserva.vehiculo_id,
            preop_id=reserva.preop_id,
            contrato_odoo_id=contrato_id,
            fecha=utc_now(),
            fecha_transaccion=data.fecha_transaccion,
            para_gastos=data.para_gastos,
            proveedor_id=proveedor.id if proveedor else None,
            proveedor_nombre=(proveedor.nombre if proveedor else data.proveedor_texto or "")[:255],
            proveedor_odoo_id=proveedor.odoo_partner_id if proveedor else None,
            galones=data.galones,
            precio_galon=data.precio_galon,
            total=data.total,
            tipo_combustible=data.tipo_combustible,
            kilometraje=data.kilometraje,
            ref_factura=data.ref_factura,
            sync_status=SyncStatus.PENDIENTE,
            next_attempt_at=utc_now(),
        )
        session.add(registro)
        try:
            session.flush()
            if data.factura:
                pdf = decode_pdf(data.factura)
                session.add(CombustibleFactura(
                    combustible_id=registro.id, nombre_archivo=(data.factura_nombre or "factura.pdf")[:255],
                    contenido=pdf, tamano=len(pdf), sha256=hashlib.sha256(pdf).hexdigest()))
            if diferido is not None:
                session.add(CombustibleEnvioDiferido(
                    uuid=registro.uuid, telegram_user_id=identity.user_id, estado=EstadoDiferido.ACEPTADO,
                    combustible_id=registro.id, apertura_at=diferido.apertura,
                    sin_senal_desde=diferido.sin_senal_desde, recibido_at=diferido.recibido,
                    retraso_segundos=diferido.retraso_segundos, payload_sha256=payload_sha256(data),
                ))
            session.flush()
        except IntegrityError as exc:  # el mismo uuid llegó dos veces en paralelo
            raise SubmissionError("Formulario ya recibido", status_code=409) from exc
        return registro, False


# ──────────────────────────────────── envío a Odoo ────────────────────────────────────

def build_odoo_values(registro: Combustible, factura: CombustibleFactura | None) -> dict[str, Any]:
    """Traduce el registro local a ``vals`` del modelo ``fuel``.

    Proveedor de la lista con contacto de Odoo -> ``proveedor`` (res.partner); escrito a mano o
    sin contacto -> ``supplier`` (texto). ``amount`` = galones × precio por galón.
    """
    vals: dict[str, Any] = {
        "employee_id": registro.empleado.odoo_id,
        "vehicle_id": registro.vehiculo.odoo_id,
        "company_id": registro.empleado.company_odoo_id,
        "date": registro.fecha_transaccion.isoformat(),
        "is_fuel_expenses": bool(registro.para_gastos),
        "gallon": float(registro.galones),
        "price_gallon": float(registro.precio_galon),
        "amount": float(registro.total),
        "fuel_type": registro.tipo_combustible if registro.tipo_combustible in TIPOS_BY_KEY else "diesel",
        "km_tanking": float(registro.kilometraje),
    }
    if registro.contrato_odoo_id:
        vals["agreement_id"] = registro.contrato_odoo_id
    if registro.proveedor_odoo_id:
        vals["proveedor"] = registro.proveedor_odoo_id
    else:
        vals["supplier"] = registro.proveedor_nombre
    if registro.ref_factura:
        vals["ref_invoice"] = registro.ref_factura
    if factura is not None:
        vals["image_bill"] = base64.b64encode(factura.contenido).decode("ascii")  # Binary: base64 sin prefijo
        vals["attachment_filename"] = factura.nombre_archivo
        vals["fuel_attachment"] = True
    return vals


@dataclass(frozen=True)
class CombustibleOutcome:
    combustible_id: int
    telegram_user_id: int
    status: SyncStatus
    odoo_id: int | None = None
    total: float = 0.0
    galones: float = 0.0
    placa: str | None = None
    error: str | None = None


def send_one(db: Database, writer: OdooWriter, combustible_id: int, max_attempts: int) -> CombustibleOutcome:
    """Envía un tanqueo ya reclamado (``enviando``) y deja el estado final en la BD."""
    with db.session() as session:
        registro = session.get(Combustible, combustible_id)
        if registro is None or registro.sync_status != SyncStatus.ENVIANDO:
            raise LookupError(f"combustible {combustible_id} no está en estado enviando")
        factura = session.scalar(select(CombustibleFactura).where(CombustibleFactura.combustible_id == registro.id))
        vals = build_odoo_values(registro, factura)
        ref, submitted_at, attempts = registro.uuid, registro.fecha, registro.attempts
        user_id, total, galones = registro.telegram_user_id, registro.total, registro.galones
        placa = reservas.placa(registro.vehiculo)

    status: SyncStatus
    error: str | None = None
    odoo_id: int | None = None
    odoo_name: str | None = None
    try:
        odoo_id, odoo_name, reused = writer.create_combustible(ref, vals, submitted_at, attempts > 1)
        status = SyncStatus.ENVIADO
        if reused:
            logger.info("Combustible %s ya existía en Odoo (id=%s): no se duplicó", combustible_id, odoo_id)
    except OdooPermanentError as exc:
        status, error = SyncStatus.FALLIDO, f"Odoo rechazó el registro: {exc}"
    except OdooTransientError as exc:
        status = SyncStatus.FALLIDO if attempts >= max_attempts else SyncStatus.ERROR
        error = f"Intento {attempts}/{max_attempts}: {exc}"

    now = utc_now()
    with db.session() as session:
        registro = session.get(Combustible, combustible_id)
        assert registro is not None
        registro.sync_status = status
        registro.error_log = error
        if status == SyncStatus.ENVIADO:
            registro.odoo_id, registro.odoo_name, registro.sent_at = odoo_id, odoo_name, now
            registro.next_attempt_at = None
        elif status == SyncStatus.ERROR:
            registro.next_attempt_at = now + preop_service.backoff(attempts)
        else:
            registro.next_attempt_at = None
    log = logger.info if status == SyncStatus.ENVIADO else logger.warning
    log("Combustible %s -> %s%s", combustible_id, status.value, f" ({error})" if error else f" odoo_id={odoo_id}")
    return CombustibleOutcome(combustible_id, user_id, status, odoo_id, float(total), float(galones), placa, error)


def claim_batch(db: Database, limit: int, lease_seconds: int, now: datetime | None = None) -> list[int]:
    return preop_service.claim_batch(db, limit, lease_seconds, now, model=Combustible)


def requeue_failed(db: Database, ids: list[int] | None = None) -> int:
    return preop_service.requeue_failed(db, ids, model=Combustible)
