"""Lógica de Gastos Operativos: contexto, recepción (incluido el envío sin señal) y envío a Odoo.

Un envío (``GastoOperativo``) lleva una fecha y N gastos. Cada gasto es **un ``hr.expense``** en Odoo
(``POST /api/gastos/register``, igual que «Combustible para gastos»). Reglas:

1. El id de Telegram es el PIN de un empleado activo (si no, 403).
2. Exige una **reserva vigente** (preoperacional de la jornada; ``reservas.reserva_vigente``): sin ella
   ``SinReservaVigenteError`` (409). El vehículo de la reserva es el que se coteja con el GPS de la flota.
3. Cada soporte (PDF o foto) llega obligatorio; una foto se convierte a PDF al recibirla.
4. El envío queda **retenido** hasta que el conductor comparte su ubicación en el chat; esa ubicación se usa en todas
   las líneas. Tras recibirla, cada línea sale por su cuenta y tiene su propio estado.

Todo es síncrono (SQLAlchemy + XML-RPC/REST); el código async lo llama con ``asyncio.to_thread``.
"""
from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from ..combustible import gps as gps_flota
from ..combustible import service as combustible_service
from ..combustible.service import RestNoConfigurado, UbicacionAsignada, RecordatorioUbicacion, enviar_gasto
from ..preop import reservas
from ..preop import service as preop_service
from ..preop.db import Database
from ..preop.diferido import motivo_vencido
from ..preop.models import (
    Empleado,
    EstadoDiferido,
    GastoOperativo,
    GastoOperativoEnvioDiferido,
    GastoOperativoLinea,
    GastoOperativoSoporte,
    SyncStatus,
    utc_now,
)
from ..preop.odoo_writer import OdooPermanentError, OdooTransientError, OdooWriter
from ..preop.rest_client import OdooRestClient
from ..preop.service import EnvioVencidoError, SubmissionError, TelegramIdentity, TiemposDiferido, payload_sha256
from . import cotejo
from . import soporte as soporte_mod
from .catalogo import TipoGasto, form_definition
from .schemas import GastosOperativosIn

logger = logging.getLogger(__name__)


# ───────────────────────────── consultas para la Mini App ─────────────────────────────

def form_full(tipos: tuple[TipoGasto, ...], topes: dict[str, int] | None = None) -> dict[str, Any]:
    return form_definition(tipos, topes)


def form_context(db: Database, empleado: Empleado, telegram_user_id: int) -> dict[str, Any]:
    """Conductor, vehículo reservado y fecha de hoy (sin reserva vigente: 409)."""
    with db.session() as session:
        v = preop_service.exigir_reserva(session, telegram_user_id).vehiculo
        vehiculo = {"id": v.id, "nombre": v.nombre, "matricula": v.matricula,
                    "numero_interno": v.numero_interno, "placa": reservas.placa(v)}
    return {
        "empleado": {"nombre": empleado.nombre, "puesto_trabajo": empleado.puesto_trabajo,
                     "responsable": empleado.responsable},
        "vehiculo": vehiculo,
        "hoy": reservas.fecha_jornada().isoformat(),
    }


# ───────────────────────────────────── recepción ──────────────────────────────────────

@dataclass(frozen=True)
class GastoRecibido:
    id: int
    uuid: str
    telegram_user_id: int
    sync_status: SyncStatus
    total: float
    gastos: int


class TopeExcedidoError(SubmissionError):
    """Un gasto supera el máximo de su tipo (``GASTOS_OPERATIVOS_TOPES``): 422 con un mensaje por gasto."""


def _pesos(valor: int) -> str:
    return "$" + f"{valor:,}".replace(",", ".")


def validar_topes(data: GastosOperativosIn, elegidos: list[TipoGasto], topes: dict[str, int] | None) -> None:
    """Rechaza (422) los gastos que superan el tope de su tipo. Sin topes configurados no hace nada."""
    if not topes:
        return
    excedidos = [
        f"Gasto #{numero} ({tipo.nombre}): {_pesos(gasto.valor)} supera el máximo de {_pesos(topes[tipo.nombre])} "
        f"por gasto"
        for numero, (gasto, tipo) in enumerate(zip(data.gastos, elegidos, strict=True), start=1)
        if tipo.nombre in topes and gasto.valor > topes[tipo.nombre]
    ]
    if excedidos:
        raise TopeExcedidoError("; ".join(excedidos))


def resumen_recibido(cab: GastoOperativo, gastos: int) -> GastoRecibido:
    return GastoRecibido(cab.id, cab.uuid, cab.telegram_user_id, cab.sync_status, float(cab.total), gastos)


def submit_diferido(db: Database, identity: TelegramIdentity, data: GastosOperativosIn, tiempos: TiemposDiferido,
                    tipos: tuple[TipoGasto, ...], topes: dict[str, int] | None = None) -> tuple[GastoRecibido, bool]:
    """Recibe un envío guardado en el teléfono por falta de señal (regla de ``app/preop/diferido.py``).

    Dentro de la ventana sigue el flujo normal; fuera de ella queda solo como auditoría (con el formulario y los
    soportes) y lanza ``EnvioVencidoError``.

    Los topes que rigen son los **vigentes al recibir** el envío (``submit`` los valida): si cambiaron mientras el
    teléfono estaba sin señal y un gasto ya los supera, se rechaza con ``TopeExcedidoError`` (422).
    """
    with db.session() as session:
        existing = session.scalar(select(GastoOperativo).where(GastoOperativo.uuid == data.uuid))
        if existing is not None:
            if existing.telegram_user_id != identity.user_id:
                raise SubmissionError("Identificador de formulario en uso", status_code=409)
            return resumen_recibido(existing, len(existing.lineas)), True
        previo = session.scalar(select(GastoOperativoEnvioDiferido).where(
            GastoOperativoEnvioDiferido.uuid == data.uuid))
        if previo is not None:
            if previo.telegram_user_id != identity.user_id:
                raise SubmissionError("Identificador de formulario en uso", status_code=409)
            raise EnvioVencidoError(previo.motivo or "Envío vencido")

    motivo = motivo_vencido(tiempos.apertura, tiempos.sin_senal_desde, tiempos.recibido)
    if motivo is None:
        return submit(db, identity, data, tipos, diferido=tiempos, topes=topes)

    with db.session() as session:
        session.add(GastoOperativoEnvioDiferido(
            uuid=data.uuid, telegram_user_id=identity.user_id, estado=EstadoDiferido.VENCIDO, motivo=motivo,
            apertura_at=tiempos.apertura, sin_senal_desde=tiempos.sin_senal_desde, recibido_at=tiempos.recibido,
            retraso_segundos=tiempos.retraso_segundos, payload_sha256=payload_sha256(data),
            payload=data.model_dump(mode="json", exclude={"diferido"}),
        ))
        try:
            session.flush()
        except IntegrityError as exc:  # el mismo uuid llegó dos veces en paralelo
            raise EnvioVencidoError(motivo) from exc
    logger.warning("Gastos operativos diferido VENCIDO uuid=%s usuario=%s: %s", data.uuid, identity.user_id, motivo)
    raise EnvioVencidoError(motivo)


def _convertir_soportes(data: GastosOperativosIn) -> list[tuple[bytes, bool, str]]:
    """PDF de cada gasto: (contenido, venía como foto, nombre del archivo). Error de foto -> 422 con el gasto."""
    out: list[tuple[bytes, bool, str]] = []
    for numero, gasto in enumerate(data.gastos, start=1):
        try:
            pdf, convertido = soporte_mod.a_pdf(soporte_mod.decodificar(gasto.soporte))
        except ValueError as exc:
            raise SubmissionError(f"Gasto #{numero}: {exc}") from exc
        out.append((pdf, convertido, soporte_mod.nombre_pdf(gasto.soporte_nombre, numero)))
    return out


def submit(db: Database, identity: TelegramIdentity, data: GastosOperativosIn, tipos: tuple[TipoGasto, ...],
           diferido: TiemposDiferido | None = None, topes: dict[str, int] | None = None) -> tuple[GastoRecibido, bool]:
    """Guarda el envío como ``pendiente`` (retenido hasta la ubicación). Devuelve (envío, era_duplicado).

    Idempotente por ``uuid``. ``diferido`` (ya validado dentro de la ventana) deja la trazabilidad sin señal.
    """
    with db.session() as session:
        existing = session.scalar(select(GastoOperativo).where(GastoOperativo.uuid == data.uuid))
        if existing is not None:
            if existing.telegram_user_id != identity.user_id:
                raise SubmissionError("Identificador de formulario en uso", status_code=409)
            return resumen_recibido(existing, len(existing.lineas)), True

    por_nombre = {t.nombre.casefold(): t for t in tipos}
    elegidos: list[TipoGasto] = []
    for numero, gasto in enumerate(data.gastos, start=1):
        tipo = por_nombre.get(gasto.tipo.casefold())
        if tipo is None:
            raise SubmissionError(f"Gasto #{numero}: el tipo «{gasto.tipo}» no existe; elige uno de la lista")
        elegidos.append(tipo)
    validar_topes(data, elegidos, topes)  # también el envío diferido: rige el tope vigente al recibir
    soportes = _convertir_soportes(data)  # CPU: antes de abrir la transacción

    with db.session() as session:
        empleado = session.scalar(
            select(Empleado).where(Empleado.codigo_pin == str(identity.user_id), Empleado.activo.is_(True))
            .order_by(Empleado.updated_at.desc()).limit(1)
        )
        if empleado is None:
            raise SubmissionError("Tu usuario de Telegram no está asociado a un empleado activo", 403)
        reserva = preop_service.exigir_reserva(session, identity.user_id, diferido)

        cab = GastoOperativo(
            uuid=data.uuid,
            telegram_user_id=identity.user_id,
            telegram_username=identity.username,
            firma_nombre=identity.full_name[:255] or empleado.nombre,
            empleado_id=empleado.id,
            vehiculo_id=reserva.vehiculo_id,
            fecha=utc_now(),
            fecha_gasto=data.fecha_gasto,
            total=float(data.total),
            ubicacion_requerida=True,  # sale a Odoo solo cuando el conductor comparte su ubicación
            sync_status=SyncStatus.PENDIENTE,
            next_attempt_at=utc_now(),
        )
        session.add(cab)
        try:
            session.flush()
            for numero, (gasto, tipo, (pdf, convertido, nombre)) in enumerate(
                    zip(data.gastos, elegidos, soportes, strict=True), start=1):
                linea = GastoOperativoLinea(
                    gasto_id=cab.id, numero=numero, tipo_nombre=tipo.nombre, product_id=tipo.product_id,
                    valor=float(gasto.valor), observacion=gasto.observacion, sync_status=SyncStatus.PENDIENTE)
                session.add(linea)
                session.flush()
                session.add(GastoOperativoSoporte(
                    linea_id=linea.id, nombre_archivo=nombre, contenido=pdf, tamano=len(pdf),
                    sha256=hashlib.sha256(pdf).hexdigest(), convertido=convertido))
            if diferido is not None:
                session.add(GastoOperativoEnvioDiferido(
                    uuid=cab.uuid, telegram_user_id=identity.user_id, estado=EstadoDiferido.ACEPTADO,
                    gasto_id=cab.id, apertura_at=diferido.apertura, sin_senal_desde=diferido.sin_senal_desde,
                    recibido_at=diferido.recibido, retraso_segundos=diferido.retraso_segundos,
                    payload_sha256=payload_sha256(data),
                ))
            session.flush()
        except IntegrityError as exc:  # el mismo uuid llegó dos veces en paralelo
            raise SubmissionError("Formulario ya recibido", status_code=409) from exc
        return resumen_recibido(cab, len(data.gastos)), False


# ──────────────────────────────────── envío a Odoo ────────────────────────────────────

def build_gasto_operativo_payload(cab: GastoOperativo, linea: GastoOperativoLinea,
                                  soporte: GastoOperativoSoporte) -> dict[str, Any]:
    """Cuerpo JSON de ``POST /api/gastos/register`` (``hr.expense``) para UNA línea (tarjeta) del envío.

    A diferencia de «Combustible para gastos» **no** lleva ``gallon``, ``fuel_type`` ni ``km_tanking``: no aplican
    y se omiten (no se mandan vacíos). ``description`` va siempre (cadena vacía sin observación).
    """
    payload: dict[str, Any] = {
        "name": linea.tipo_nombre.upper(),
        "product_id": linea.product_id,
        "total_amount": float(linea.valor),
        "employee_id": cab.empleado.odoo_id,
        "description": linea.observacion or "",
        "date": cab.fecha_gasto.isoformat(),
        "is_fuel_expenses": False,
        "type_file": "pdf",
        "attachment_filename": soporte.nombre_archivo,
        "attachment": base64.b64encode(soporte.contenido).decode("ascii"),
    }
    if cab.latitud is not None and cab.longitud is not None:
        # hr.expense no tiene lat/long: la ubicación viaja como texto (y el cotejo con el GPS de la flota).
        payload["ubicacion_gps_telegram"] = gps_flota.texto_telegram(cab.latitud, cab.longitud)
        payload["ubicacion_gps_vehiculo"] = cab.ubicacion_gps_vehiculo or gps_flota.NO_DISPONIBLE
    return payload


@dataclass(frozen=True)
class GastoFallido:
    numero: int
    tipo: str
    valor: float
    error: str | None = None


@dataclass(frozen=True)
class GastosOutcome:
    gasto_id: int
    telegram_user_id: int
    status: SyncStatus
    enviados: int = 0
    total_gastos: int = 0
    fallidos: tuple[GastoFallido, ...] = ()
    total: float = 0.0
    placa: str | None = None
    error: str | None = None


def _guardar_linea(db: Database, linea_id: int, status: SyncStatus, error: str | None,
                   odoo_id: int | None = None) -> None:
    with db.session() as session:
        linea = session.get(GastoOperativoLinea, linea_id)
        assert linea is not None
        linea.sync_status, linea.error_log = status, error
        if status == SyncStatus.ENVIADO:
            linea.odoo_gasto_id, linea.sent_at = odoo_id, utc_now()


def send_one(db: Database, writer: OdooWriter, gasto_id: int, max_attempts: int,
             rest_client: OdooRestClient | None = None,
             gps: gps_flota.FlotaGpsClient | None = None) -> GastosOutcome:
    """Envía un envío ya reclamado (``enviando``): un ``hr.expense`` por cada línea aún no enviada.

    Cada línea guarda su resultado apenas lo tiene (un corte a mitad no pierde los ids ya creados). Un 4xx deja
    la línea en ``fallido`` (definitivo); 5xx o red la dejan en ``error`` y el envío se reintenta con backoff
    (solo las líneas pendientes) hasta ``max_attempts``. El envío termina ``enviado`` si todas las líneas lo están
    y ``fallido`` si alguna quedó fallida.
    """
    with db.session() as session:
        cab = session.get(GastoOperativo, gasto_id)
        if cab is None or cab.sync_status != SyncStatus.ENVIANDO:
            raise LookupError(f"gasto operativo {gasto_id} no está en estado enviando")
    tomar_instantanea_gps(db, gasto_id, gps)  # antes de armar los payloads; el GPS nunca retiene el envío

    with db.session() as session:
        cab = session.get(GastoOperativo, gasto_id)
        assert cab is not None
        attempts, user_id, total = cab.attempts, cab.telegram_user_id, float(cab.total)
        placa = reservas.placa(cab.vehiculo) if cab.vehiculo is not None else None
        usados = {int(ln.odoo_gasto_id) for ln in cab.lineas if ln.odoo_gasto_id}
        trabajo: list[tuple[int, int, dict[str, Any] | None]] = []  # (linea_id, numero, payload)
        for ln in cab.lineas:
            if ln.sync_status not in (SyncStatus.PENDIENTE, SyncStatus.ERROR):
                continue
            soporte = session.scalar(select(GastoOperativoSoporte).where(GastoOperativoSoporte.linea_id == ln.id))
            trabajo.append((ln.id, ln.numero,
                            build_gasto_operativo_payload(cab, ln, soporte) if soporte is not None else None))

    sin_cliente = False
    ultimo_transitorio: str | None = None
    for linea_id, numero, payload in trabajo:
        try:
            if payload is None:
                raise OdooPermanentError("El gasto no tiene soporte adjunto")
            odoo_id = enviar_gasto(
                writer, rest_client, payload, linea_id,
                buscar=lambda p: writer.buscar_gasto_operativo(p, excluir_ids=usados),
                etiqueta=f"Gasto operativo {gasto_id} línea")
        except RestNoConfigurado as exc:
            # Es de configuración, no del registro: no consume intentos ni pasa a fallido.
            sin_cliente, ultimo_transitorio = True, str(exc)
            logger.error("Gasto operativo %s en espera: %s", gasto_id, exc)
            break
        except OdooPermanentError as exc:
            _guardar_linea(db, linea_id, SyncStatus.FALLIDO, f"Odoo rechazó el gasto #{numero}: {exc}")
        except OdooTransientError as exc:
            agotado = attempts >= max_attempts
            ultimo_transitorio = f"Intento {attempts}/{max_attempts}: {exc}"
            _guardar_linea(db, linea_id, SyncStatus.FALLIDO if agotado else SyncStatus.ERROR, ultimo_transitorio)
        else:
            if odoo_id is not None:
                usados.add(odoo_id)
            _guardar_linea(db, linea_id, SyncStatus.ENVIADO, None, odoo_id)

    now = utc_now()
    with db.session() as session:
        cab = session.get(GastoOperativo, gasto_id)
        assert cab is not None
        lineas = list(cab.lineas)
        enviados = sum(1 for ln in lineas if ln.sync_status == SyncStatus.ENVIADO)
        fallidos = tuple(GastoFallido(ln.numero, ln.tipo_nombre, float(ln.valor), ln.error_log)
                         for ln in lineas if ln.sync_status == SyncStatus.FALLIDO)
        abiertas = any(ln.sync_status in (SyncStatus.PENDIENTE, SyncStatus.ERROR) for ln in lineas)
        error: str | None = None
        if sin_cliente:
            status, error = SyncStatus.ERROR, ultimo_transitorio
            cab.attempts = max(cab.attempts - 1, 0)
            cab.next_attempt_at = now + preop_service.backoff(1) * 10
        elif abiertas:
            status, error = SyncStatus.ERROR, ultimo_transitorio
            cab.next_attempt_at = now + preop_service.backoff(attempts)
        else:
            status = SyncStatus.ENVIADO if not fallidos else SyncStatus.FALLIDO
            error = "; ".join(f"#{f.numero} {f.tipo}: {f.error}" for f in fallidos) or None
            cab.next_attempt_at = None
            if status == SyncStatus.ENVIADO:
                cab.sent_at = now
        cab.sync_status, cab.error_log = status, error
        outcome = GastosOutcome(gasto_id, user_id, status, enviados, len(lineas), fallidos, total, placa, error)
    log = logger.info if status == SyncStatus.ENVIADO else logger.warning
    log("Gasto operativo %s -> %s (%s/%s gastos)%s", gasto_id, status.value, enviados, len(lineas),
        f" {error}" if error else "")
    return outcome


def claim_batch(db: Database, limit: int, lease_seconds: int, now: datetime | None = None) -> list[int]:
    """Reclama los envíos listos; los que esperan la ubicación del conductor quedan retenidos."""
    libre = or_(GastoOperativo.ubicacion_requerida.is_not(True), GastoOperativo.latitud.is_not(None))
    return preop_service.claim_batch(db, limit, lease_seconds, now, model=GastoOperativo, where=libre)


def requeue_failed(db: Database, ids: list[int] | None = None) -> int:
    """Devuelve a ``pendiente`` los envíos ``fallido`` y sus líneas fallidas; las ya enviadas no se tocan."""
    with db.session() as session:
        stmt = select(GastoOperativo.id).where(GastoOperativo.sync_status == SyncStatus.FALLIDO)
        if ids:
            stmt = stmt.where(GastoOperativo.id.in_(ids))
        afectados = list(session.scalars(stmt))
        if not afectados:
            return 0
        session.execute(
            update(GastoOperativoLinea)
            .where(GastoOperativoLinea.gasto_id.in_(afectados),
                   GastoOperativoLinea.sync_status.in_([SyncStatus.FALLIDO, SyncStatus.ERROR]))
            .values(sync_status=SyncStatus.PENDIENTE, error_log=None)
            .execution_options(synchronize_session=False))
        # attempts=1: el siguiente envío busca antes de postear (un fallido por timeout pudo crearse en Odoo).
        session.execute(
            update(GastoOperativo).where(GastoOperativo.id.in_(afectados))
            .values(sync_status=SyncStatus.PENDIENTE, attempts=1, next_attempt_at=utc_now(), error_log=None)
            .execution_options(synchronize_session=False))
    return len(afectados)


# ───────────────────────────────── ubicación del envío ─────────────────────────────────
# Misma mecánica que Combustible (``combustible.service``), aplicada a la tabla ``gastos_operativos``.

def tomar_instantanea_gps(db: Database, gasto_id: int, gps: gps_flota.FlotaGpsClient | None) -> None:
    """Cotejo con el GPS de la flota (una sola vez por envío); sin vehículo o con fallo: ``GPS no disponible``."""
    combustible_service.tomar_instantanea_gps(db, gasto_id, gps, model=GastoOperativo)


def asignar_ubicacion(db: Database, telegram_user_id: int, latitud: float, longitud: float,
                      enviada_at: datetime, now: datetime | None = None) -> UbicacionAsignada | None:
    res = combustible_service.asignar_ubicacion(db, telegram_user_id, latitud, longitud, enviada_at, now,
                                                model=GastoOperativo)
    if res is None:
        return None
    cotejo.cotejar_seguro(db, res.combustible_id)  # solo se almacena; un fallo no frena el envío
    return replace(res, origen="gastos")


def recordatorios_pendientes(db: Database, now: datetime | None = None) -> list[RecordatorioUbicacion]:
    return [replace(r, origen="gastos")
            for r in combustible_service.recordatorios_pendientes(db, now, model=GastoOperativo)]


def placa_de(db: Database, gasto_id: int) -> str | None:
    return combustible_service.placa_de(db, gasto_id, model=GastoOperativo)


def fecha_candidata_ubicacion(db: Database, telegram_user_id: int, enviada_at: datetime) -> datetime | None:
    return combustible_service.fecha_candidata_ubicacion(db, telegram_user_id, enviada_at, model=GastoOperativo)


def contar_ubicaciones_pendientes(db: Database, telegram_user_id: int) -> int:
    return combustible_service.contar_ubicaciones_pendientes(db, telegram_user_id, model=GastoOperativo)


def asignar_ubicacion_mas_antigua(db: Database, telegram_user_id: int, latitud: float, longitud: float,
                                  enviada_at: datetime, now: datetime | None = None) -> UbicacionAsignada | None:
    """Reparte la ubicación entre Combustible y Gastos Operativos: va al pendiente MÁS ANTIGUO de los dos.

    Cada módulo solo cuenta lo que se recibió antes de la ubicación (``enviada_at``). Con empate gana Combustible.
    ``pendientes`` suma lo que sigue esperando ubicación en ambos módulos. None si ninguno la espera.
    """
    fecha_c = combustible_service.fecha_candidata_ubicacion(db, telegram_user_id, enviada_at)
    fecha_g = fecha_candidata_ubicacion(db, telegram_user_id, enviada_at)
    if fecha_c is None and fecha_g is None:
        return None
    if fecha_g is None or (fecha_c is not None and fecha_c <= fecha_g):
        asignada = combustible_service.asignar_ubicacion(db, telegram_user_id, latitud, longitud, enviada_at, now)
        otros = contar_ubicaciones_pendientes(db, telegram_user_id)
    else:
        asignada = asignar_ubicacion(db, telegram_user_id, latitud, longitud, enviada_at, now)
        otros = combustible_service.contar_ubicaciones_pendientes(db, telegram_user_id)
    return replace(asignada, pendientes=asignada.pendientes + otros) if asignada is not None else None
