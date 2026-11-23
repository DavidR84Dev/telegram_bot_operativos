"""Lógica de negocio del preoperacional: recepción, cola de envío y consultas de la Mini App.

Todo es síncrono (SQLAlchemy + XML-RPC); el código async lo llama con
``asyncio.to_thread`` para no bloquear el event loop de FastAPI/Telegram.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .checklist import CHECKLIST, NO_CONFORME, count_no_conformes, labrado_minimo_acuerdo, llantas_bajo_minimo
from .db import Database
from .models import Acuerdo, Empleado, Extracto, Flota, Preoperacional, PreopFoto, SyncStatus, Ubicacion, utc_now
from .odoo_writer import OdooPermanentError, OdooTransientError, OdooWriter, build_odoo_values
from .schemas import PreoperacionalIn, decode_photo

logger = logging.getLogger(__name__)

BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 3600
# Estados de car.extractos que habilitan el preoperacional ("Aprobada" en Odoo).
ESTADOS_EXTRACTO_APROBADO = frozenset({"approved"})
# Margen de vigencia del extracto a cada lado de "ahora" (turnos que cruzan medianoche).
VIGENCIA_MARGEN = timedelta(days=1)
# Una ubicación enviada desde el chat se asocia al preoperacional de los últimos N minutos.
UBICACION_VENTANA = timedelta(minutes=10)
FOTO_TESTIGOS = "testigos"


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


def normalize_name(value: str | None) -> str:
    """Nombre comparable: sin tildes, en mayúsculas y con espacios simples."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.upper().split())


def extractos_del_conductor(session: Session, empleado: Empleado, now: datetime | None = None) -> list[Extracto]:
    """Extractos **aprobados** y vigentes de la compañía del empleado en los que figura como conductor.

    La API entrega el conductor por nombre (conductor, segundo o tercer conductor):
    se compara normalizado. Cuando ``viacotur_api`` exponga el id del conductor,
    esta comparación debe pasar a ser por id.
    """
    now = now or utc_now()
    candidatos = session.scalars(
        select(Extracto).where(
            Extracto.activo.is_(True),
            Extracto.company_odoo_id == empleado.company_odoo_id,
            Extracto.estado.in_(ESTADOS_EXTRACTO_APROBADO),
            Extracto.vehiculo_id.is_not(None),
            or_(Extracto.fecha_hora_inicio.is_(None), Extracto.fecha_hora_inicio <= now + VIGENCIA_MARGEN),
            or_(Extracto.fecha_hora_fin.is_(None), Extracto.fecha_hora_fin >= now - VIGENCIA_MARGEN),
        ).order_by(Extracto.fecha_hora_inicio.desc())
    ).unique().all()
    nombre = normalize_name(empleado.nombre)
    return [
        e for e in candidatos
        if nombre and nombre in {normalize_name(c) for c in (e.conductor, e.segundo_conductor, e.tercer_conductor)}
    ]


def _contratos_empleado(empleado: Empleado) -> list[dict[str, Any]]:
    return [
        {"id_contrato": cid, "nombre": nombre}
        for cid, nombre in ((empleado.id_contract, empleado.contrato),
                            (empleado.contrato_temporal_id, empleado.contrato_temporal))
        if cid
    ]


def _contratos_permitidos(extracto: Extracto, empleado: Empleado) -> list[dict[str, Any]]:
    """Contratos del extracto; si el extracto no trae ninguno, los del empleado."""
    contratos = [c for c in extracto.contratos_list if c.get("id_contrato")]
    return contratos or _contratos_empleado(empleado)


def minimo_acuerdo(session: Session, contrato_odoo_id: int | None, tipo_vehiculo: str | None) -> float | None:
    """Labrado mínimo que fija el acuerdo (agreement) para el tipo de vehículo; None si no lo define."""
    if not contrato_odoo_id:
        return None
    acuerdo = session.scalar(select(Acuerdo).where(Acuerdo.odoo_id == contrato_odoo_id))
    if acuerdo is None:
        return None
    return labrado_minimo_acuerdo(tipo_vehiculo, acuerdo.labrado_camioneta, acuerdo.labrado_buses)


# Lector del último odómetro en Odoo (``OdooWriter.ultimo_odometro``): id de Odoo del vehículo -> km.
OdometroReader = Callable[[int], "float | None"]


def ultimo_odometro(vehiculo: Flota, odometro: OdometroReader | None) -> float | None:
    """Último odómetro registrado: en vivo desde Odoo; si no responde, el sincronizado por la API."""
    valor = odometro(vehiculo.odoo_id) if odometro is not None else None
    if valor is None and vehiculo.odometer_post and vehiculo.odometer_post > 0:
        valor = float(vehiculo.odometer_post)
    return valor


def form_context(db: Database, empleado: Empleado, now: datetime | None = None,
                 odometro: OdometroReader | None = None) -> dict[str, Any]:
    """Datos del paso 1 de la Mini App: conductor, responsable y extractos aprobados con su vehículo.

    La Mini App muestra la unión de los contratos y, al elegir uno, los vehículos de los
    extractos que lo incluyen (uno o varios). Cada contrato trae el labrado mínimo del
    acuerdo según el tipo de vehículo del extracto (None = se usa el de Odoo).
    """
    with db.session() as session:
        extractos = extractos_del_conductor(session, empleado, now)
        items = []
        for e in extractos:
            v = e.flota
            tipo = v.tipo_solicitud if v else None
            items.append({
                "id": e.id, "codigo": e.codigo, "estado": e.estado,
                "fecha_hora_inicio": e.fecha_hora_inicio.isoformat() if e.fecha_hora_inicio else None,
                "fecha_hora_fin": e.fecha_hora_fin.isoformat() if e.fecha_hora_fin else None,
                "contratos": [
                    {**c, "labrado_minimo": minimo_acuerdo(session, c["id_contrato"], tipo)}
                    for c in _contratos_permitidos(e, empleado)
                ],
                "vehiculo": {
                    "tipo": tipo,
                    "id": v.id if v and v.activo else None,
                    "odoo_id": e.vehiculo_id,
                    "nombre": v.nombre if v else e.vehiculo,
                    "matricula": v.matricula if v else None,
                    "numero_interno": v.numero_interno if v else None,
                    # Sugerencia "Último registrado" (fleet.vehicle.odometer_post en Odoo).
                    "odometer_post": ultimo_odometro(v, odometro) if v else None,
                },
            })
    return {
        "empleado": {
            "nombre": empleado.nombre, "puesto_trabajo": empleado.puesto_trabajo,
            "responsable": empleado.responsable,
        },
        "validado": bool(items),
        "extractos": items,
    }


# ───────────────────────────────────── recepción ──────────────────────────────────────

def submit(db: Database, identity: TelegramIdentity, data: PreoperacionalIn,
           odometro: OdometroReader | None = None) -> tuple[Preoperacional, bool]:
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

        aprobados = {e.id: e for e in extractos_del_conductor(session, empleado)}
        extracto = aprobados.get(data.extracto_id)
        if extracto is None:
            raise SubmissionError("El extracto no está aprobado y vigente, o no estás registrado como su conductor", 403)
        vehiculo = session.scalar(select(Flota).where(Flota.odoo_id == extracto.vehiculo_id, Flota.activo.is_(True)))
        if vehiculo is None:
            raise SubmissionError("El vehículo del extracto aún no está sincronizado; intenta en unos minutos", 409)
        if data.vehiculo_id is not None and data.vehiculo_id != vehiculo.id:
            raise SubmissionError("El vehículo no corresponde al extracto")

        permitidos = [c["id_contrato"] for c in _contratos_permitidos(extracto, empleado)]
        contrato = data.contrato_odoo_id
        if contrato is not None and contrato not in permitidos:
            raise SubmissionError("El contrato no corresponde al extracto")
        if contrato is None and len(permitidos) == 1:
            contrato = permitidos[0]
        if contrato is None and len(permitidos) > 1:
            raise SubmissionError("Selecciona el contrato")

        ultimo = ultimo_odometro(vehiculo, odometro)
        if ultimo is not None and data.kilometraje < ultimo:
            raise SubmissionError(
                f"El odómetro ({data.kilometraje:,.0f} km) debe ser mayor o igual al último registrado "
                f"({ultimo:,.0f} km)".replace(",", "."))

        foto, mime = decode_photo(data.testigos_img) if data.testigos_img else (None, None)

        preop = Preoperacional(
            uuid=data.uuid,
            telegram_user_id=identity.user_id,
            telegram_username=identity.username,
            firma_nombre=identity.full_name[:255] or empleado.nombre,
            empleado_id=empleado.id,
            vehiculo_id=vehiculo.id,
            extracto_id=extracto.id,
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
            if foto is not None:
                session.add(PreopFoto(
                    preop_id=preop.id, tipo=FOTO_TESTIGOS, mime=mime, contenido=foto, tamano=len(foto),
                    sha256=hashlib.sha256(foto).hexdigest(),
                ))
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
class AvisoJefe:
    """Datos del aviso por Telegram al jefe inmediato cuando hay hallazgos."""

    telegram_id: int
    conductor: str
    vehiculo: str
    no_conformes: tuple[str, ...]
    llantas_bajas: tuple[str, ...]


@dataclass(frozen=True)
class SendOutcome:
    preop_id: int
    telegram_user_id: int
    status: SyncStatus
    odoo_name: str | None = None
    no_conformes: int = 0
    error: str | None = None
    aviso_jefe: AvisoJefe | None = None


def _boss_odoo_id(session: Session, empleado: Empleado) -> int | None:
    """Jefe inmediato: la API entrega solo el nombre del responsable; se resuelve si es único."""
    jefe = _jefe(session, empleado)
    return jefe.odoo_id if jefe else None


def _jefe(session: Session, empleado: Empleado) -> Empleado | None:
    """Jefe inmediato (por nombre del responsable, solo si es único y activo)."""
    if not empleado.responsable:
        return None
    jefes = session.scalars(
        select(Empleado).where(Empleado.nombre == empleado.responsable, Empleado.activo.is_(True)).limit(2)
    ).all()
    return jefes[0] if len(jefes) == 1 else None


def _aviso_jefe(session: Session, preop: Preoperacional) -> AvisoJefe | None:
    """Aviso para el jefe si hay ítems no conformes o llantas bajo el labrado del acuerdo.

    Requiere que el jefe tenga su id de Telegram en el PIN (``hr.employee.pin``), igual
    que los conductores; si no, no se avisa (queda en el log).
    """
    vehiculo = preop.vehiculo
    no_conformes = tuple(i.label for i in CHECKLIST if preop.checklist.get(i.key) == NO_CONFORME)
    llantas = tuple(llantas_bajo_minimo(
        preop.llantas, minimo_acuerdo(session, preop.contrato_odoo_id, vehiculo.tipo_solicitud)))
    if not no_conformes and not llantas:
        return None
    jefe = _jefe(session, preop.empleado)
    pin = jefe.codigo_pin if jefe else None
    if not pin or not pin.isdigit():
        logger.warning("Preoperacional %s con hallazgos: el jefe (%s) no tiene id de Telegram en el PIN",
                       preop.id, preop.empleado.responsable or "sin responsable")
        return None
    return AvisoJefe(int(pin), preop.empleado.nombre,
                     " · ".join(x for x in (vehiculo.matricula, vehiculo.nombre) if x), no_conformes, llantas)


def send_one(db: Database, writer: OdooWriter, preop_id: int, max_attempts: int) -> SendOutcome:
    """Envía un registro ya reclamado (``enviando``) y deja el estado final en la BD."""
    with db.session() as session:
        preop = session.get(Preoperacional, preop_id)
        if preop is None or preop.sync_status != SyncStatus.ENVIANDO:
            raise LookupError(f"preoperacional {preop_id} no está en estado enviando")
        foto = session.scalar(select(PreopFoto.contenido).where(
            PreopFoto.preop_id == preop.id, PreopFoto.tipo == FOTO_TESTIGOS))
        vals = build_odoo_values(preop, _boss_odoo_id(session, preop.empleado),
                                 testigos_b64=base64.b64encode(foto).decode("ascii") if foto else None)
        ref, submitted_at, attempts = preop.uuid, preop.fecha, preop.attempts
        aviso = _aviso_jefe(session, preop)
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
    return SendOutcome(preop_id, user_id, status, odoo_name, no_conformes, error,
                       aviso if status == SyncStatus.ENVIADO else None)


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


# ──────────────────────────────────── ubicación ───────────────────────────────────────

@dataclass(frozen=True)
class UbicacionRegistrada:
    id: int
    preop_id: int | None
    preop_nombre: str | None  # PRE-xxxx si ya está en Odoo


def registrar_ubicacion(db: Database, telegram_user_id: int, latitud: float, longitud: float,
                        precision_m: float | None = None, now: datetime | None = None) -> UbicacionRegistrada:
    """Guarda la ubicación compartida desde el chat y la asocia al preoperacional reciente del usuario."""
    now = now or utc_now()
    with db.session() as session:
        empleado = session.scalar(
            select(Empleado).where(Empleado.codigo_pin == str(telegram_user_id), Empleado.activo.is_(True))
            .order_by(Empleado.updated_at.desc()).limit(1)
        )
        preop = session.scalar(
            select(Preoperacional).where(
                Preoperacional.telegram_user_id == telegram_user_id,
                Preoperacional.created_at >= now - UBICACION_VENTANA,
            ).order_by(Preoperacional.created_at.desc()).limit(1)
        )
        ubicacion = Ubicacion(
            telegram_user_id=telegram_user_id, empleado_id=empleado.id if empleado else None,
            preop_id=preop.id if preop else None, latitud=latitud, longitud=longitud,
            precision_m=precision_m, recibido_at=now,
        )
        session.add(ubicacion)
        session.flush()
        return UbicacionRegistrada(ubicacion.id, preop.id if preop else None, preop.odoo_name if preop else None)
