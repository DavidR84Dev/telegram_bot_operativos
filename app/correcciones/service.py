"""Lógica de Correcciones Post y Gastos: lista en vivo desde Odoo, recepción (incluido el envío sin señal) y envío.

Dos clases de corrección:

* **Gasto** (``hr.expense`` rechazado, ``state_expenses`` = ``refused``): se envía un ``hr.expense`` NUEVO por
  ``POST /api/gastos/register`` con ``state_expenses`` = ``correction``. No edita ni enlaza el original, que queda
  rechazado.
* **Post-operacional** (``pos.operacional`` en ``refused`` / ``refused by``): se escribe (XML-RPC ``write``) sobre ese
  mismo registro y pasa a ``correct`` / ``correct by``.

Reglas: el id de Telegram es el PIN de un empleado activo (403); el registro a corregir debe ser del empleado y seguir
pendiente en Odoo (se vuelve a leer al recibir y al enviar). **No exige reserva vigente**: la corrección es posterior
a la jornada. Todo es síncrono (SQLAlchemy + XML-RPC/REST); el código async lo llama con ``asyncio.to_thread``.
"""
from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..combustible.service import RestNoConfigurado, enviar_gasto
from ..gastos_operativos import soporte as soporte_mod
from ..pos_operacional.service import _hora_colombia, nombre_planilla
from ..preop import reservas
from ..preop import service as preop_service
from ..preop.db import Database
from ..preop.diferido import motivo_vencido
from ..preop.models import Correccion, CorreccionEnvioDiferido, Empleado, EstadoDiferido, SyncStatus, utc_now
from ..preop.odoo_writer import (
    POS_CAMPOS_LECTURA,
    OdooPermanentError,
    OdooTransientError,
    OdooWriteError,
    OdooWriter,
    _odoo_datetime,
)
from ..preop.rest_client import OdooRestClient
from ..preop.schemas import related_id, related_name
from ..preop.service import EnvioVencidoError, SubmissionError, TelegramIdentity, TiemposDiferido, payload_sha256
from . import catalogo as cat
from .catalogo import form_definition
from .schemas import CorreccionGastoIn, CorreccionPosIn

logger = logging.getLogger(__name__)

__all__ = ["form_definition"]

# Separador entre las observaciones que ya tiene el Post-operacional y la corrección que se agrega al final.
SEPARADOR_OBSERVACIONES = "\n\n"
# Una corrección "en curso" (o ya enviada) oculta el registro original de la lista y bloquea una segunda corrección.
ESTADOS_ACTIVOS = (SyncStatus.PENDIENTE, SyncStatus.ENVIANDO, SyncStatus.ERROR, SyncStatus.ENVIADO)
MSG_ODOO_NO_RESPONDE = "No se pudo consultar Odoo en este momento. Intenta de nuevo en unos minutos."
CAMPOS_GASTO = ["name", "product_id", "total_amount", "employee_id", "date", "description", "state_expenses"]


# ───────────────────────────── consultas para la Mini App ─────────────────────────────

def _texto(valor: Any) -> str:
    """Los campos vacíos de Odoo llegan como ``False``: se muestran como texto vacío."""
    return "" if valor is False or valor is None else str(valor)


def _hora_local_odoo(valor: Any) -> str:
    """``check_in``/``check_out`` de Odoo (UTC naive ``YYYY-MM-DD HH:MM:SS``) -> ``YYYY-MM-DDTHH:MM`` de Colombia."""
    if not valor:
        return ""
    try:
        return _hora_colombia(datetime.strptime(str(valor)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC))
    except ValueError:
        return ""


def _fecha_desde() -> date:
    """Primer día (hora de Colombia) que entra en la ventana de ``CORRECCIONES_DIAS_MAXIMOS`` días hacia atrás."""
    return reservas.fecha_jornada() - timedelta(days=cat.dias_maximos())


def _fecha_pos(fila: dict[str, Any]) -> date | None:
    """Fecha (Colombia) del Post-operacional: ``check_in`` o, si está vacío, ``create_date``."""
    for campo in ("check_in", "create_date"):
        local = _hora_local_odoo(fila.get(campo))
        if local:
            return date.fromisoformat(local[:10])
    return None


def _ids_en_curso(session: Session, tipo: str) -> set[int]:
    return set(session.scalars(select(Correccion.odoo_id_original).where(
        Correccion.tipo == tipo, Correccion.sync_status.in_(ESTADOS_ACTIVOS), Correccion.descartado.is_(False))))


def _item_gasto(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]), "nombre": _texto(row.get("name")), "tipo": related_name(row.get("product_id")) or "",
        "valor": float(row.get("total_amount") or 0), "fecha": _texto(row.get("date")),
        "descripcion": _texto(row.get("description")),
    }


def _item_pos(row: dict[str, Any]) -> dict[str, Any]:
    estado = _texto(row.get("state"))
    return {
        "id": int(row["id"]), "nombre": _texto(row.get("name")), "estado": estado,
        "estado_texto": cat.POS_ESTADO_TEXTO.get(estado, estado.upper()),
        "entrada": _hora_local_odoo(row.get("check_in")), "salida": _hora_local_odoo(row.get("check_out")),
        "vehiculo": related_name(row.get("vehicle_id")) or "", "contrato": related_name(row.get("agreement_id")) or "",
        "km_inicial": float(row.get("km_start") or 0), "km_final": float(row.get("km_end") or 0),
        "observaciones": _texto(row.get("observations")), "planilla_nombre": _texto(row.get("attachment_filename")),
        # Anotaciones de quien devolvió el registro (gastos / nómina), para que el conductor sepa qué corregir.
        "revision": " | ".join(t for t in (_texto(row.get("corrections")), _texto(row.get("corrections_hr"))) if t),
    }


def contexto(db: Database, writer: OdooWriter, empleado: Empleado) -> dict[str, Any]:
    """Conductor y sus registros corregibles, leídos en vivo de Odoo (sin los que ya tienen una corrección en curso).

    Si Odoo no responde devuelve ``odoo_ok`` falso con un mensaje claro (la Mini App lo muestra; no es un error HTTP).
    """
    desde = _fecha_desde()
    gastos = writer.gastos_rechazados(empleado.odoo_id, desde)
    pos = writer.pos_pendientes(empleado.odoo_id, desde)
    base = {"empleado": {"nombre": empleado.nombre, "puesto_trabajo": empleado.puesto_trabajo,
                         "responsable": empleado.responsable},
            "hoy": reservas.fecha_jornada().isoformat(), "dias_maximos": cat.dias_maximos(),
            "desde": desde.isoformat()}
    if gastos is None or pos is None:
        return {**base, "odoo_ok": False, "mensaje": MSG_ODOO_NO_RESPONDE, "gastos": [], "pos": []}
    with db.session() as session:
        en_gastos, en_pos = _ids_en_curso(session, cat.TIPO_GASTO), _ids_en_curso(session, cat.TIPO_POS)
    return {**base, "odoo_ok": True, "mensaje": None,
            "gastos": [_item_gasto(g) for g in gastos if int(g["id"]) not in en_gastos],
            "pos": [_item_pos(p) for p in pos if int(p["id"]) not in en_pos]}


# ───────────────────────────────────── recepción ──────────────────────────────────────

@dataclass(frozen=True)
class CorreccionRecibida:
    id: int
    uuid: str
    telegram_user_id: int
    tipo: str
    sync_status: SyncStatus
    referencia: str | None = None


def resumen_recibido(c: Correccion) -> CorreccionRecibida:
    return CorreccionRecibida(c.id, c.uuid, c.telegram_user_id, c.tipo, c.sync_status, c.referencia)


def submit_diferido(db: Database, writer: OdooWriter, identity: TelegramIdentity, data: CorreccionGastoIn | CorreccionPosIn,
                    tiempos: TiemposDiferido) -> tuple[CorreccionRecibida, bool]:
    """Recibe un envío guardado en el teléfono por falta de señal (regla de ``app/preop/diferido.py``).

    Dentro de la ventana sigue el flujo normal; fuera de ella queda solo como auditoría (con el formulario y el PDF)
    y lanza ``EnvioVencidoError``.
    """
    with db.session() as session:
        existing = session.scalar(select(Correccion).where(Correccion.uuid == data.uuid))
        if existing is not None:
            if existing.telegram_user_id != identity.user_id:
                raise SubmissionError("Identificador de formulario en uso", status_code=409)
            return resumen_recibido(existing), True
        previo = session.scalar(select(CorreccionEnvioDiferido).where(CorreccionEnvioDiferido.uuid == data.uuid))
        if previo is not None:
            if previo.telegram_user_id != identity.user_id:
                raise SubmissionError("Identificador de formulario en uso", status_code=409)
            raise EnvioVencidoError(previo.motivo or "Envío vencido")

    motivo = motivo_vencido(tiempos.apertura, tiempos.sin_senal_desde, tiempos.recibido)
    if motivo is None:
        return submit(db, writer, identity, data, diferido=tiempos)

    with db.session() as session:
        session.add(CorreccionEnvioDiferido(
            uuid=data.uuid, telegram_user_id=identity.user_id, estado=EstadoDiferido.VENCIDO, motivo=motivo,
            apertura_at=tiempos.apertura, sin_senal_desde=tiempos.sin_senal_desde, recibido_at=tiempos.recibido,
            retraso_segundos=tiempos.retraso_segundos, payload_sha256=payload_sha256(data),
            payload=data.model_dump(mode="json", exclude={"diferido"}),
        ))
        try:
            session.flush()
        except IntegrityError as exc:  # el mismo uuid llegó dos veces en paralelo
            raise EnvioVencidoError(motivo) from exc
    logger.warning("Corrección diferida VENCIDA uuid=%s usuario=%s: %s", data.uuid, identity.user_id, motivo)
    raise EnvioVencidoError(motivo)


def _leer_original(writer: OdooWriter, data: CorreccionGastoIn | CorreccionPosIn, empleado: Empleado) -> dict[str, Any]:
    """Relee el registro a corregir en Odoo y comprueba que sea del conductor y siga pendiente.

    Errores: 404 no existe, 403 es de otro empleado, 409 ya no está pendiente, 503 Odoo no responde (la Mini App lo
    trata como falta de señal y lo reintenta).
    """
    es_gasto = data.tipo == cat.TIPO_GASTO
    modelo = "hr.expense" if es_gasto else "pos.operacional"
    try:
        fila = writer.leer_registro(modelo, data.odoo_id, CAMPOS_GASTO if es_gasto else POS_CAMPOS_LECTURA)
    except OdooWriteError as exc:
        logger.warning("Corrección: no se pudo leer %s %s en Odoo: %s", modelo, data.odoo_id, exc)
        raise SubmissionError(MSG_ODOO_NO_RESPONDE, status_code=503) from exc
    if fila is None:
        raise SubmissionError("El registro que quieres corregir ya no existe en Odoo", status_code=404)
    if related_id(fila.get("employee_id")) != empleado.odoo_id:
        raise SubmissionError("Ese registro no es tuyo", status_code=403)
    if es_gasto:
        if fila.get("state_expenses") != cat.GASTO_RECHAZADO:
            raise SubmissionError("Ese gasto ya no está rechazado: no necesita corrección", status_code=409)
        fecha_original = _texto(fila.get("date"))[:10]
        fuera = not fecha_original or date.fromisoformat(fecha_original) < _fecha_desde()
    else:
        if fila.get("state") not in cat.ESTADOS_POS_PENDIENTES:
            raise SubmissionError("Ese Post-operacional ya no está pendiente de corrección", status_code=409)
        fecha_pos = _fecha_pos(fila)
        fuera = fecha_pos is not None and fecha_pos < _fecha_desde()
    if fuera:
        raise SubmissionError(f"Ese registro tiene más de {cat.dias_maximos()} días de antigüedad: "
                              "ya no se puede corregir desde aquí", status_code=422)
    return fila


def _a_pdf(valor: str) -> bytes:
    """Soporte recibido (PDF o foto JPG/PNG en base64) -> PDF. Una foto ilegible es un 422."""
    try:
        pdf, convertido = soporte_mod.a_pdf(soporte_mod.decodificar(valor))
    except ValueError as exc:
        raise SubmissionError(str(exc), status_code=422) from exc
    if convertido:
        logger.info("Corrección: foto convertida a PDF (%d bytes)", len(pdf))
    return pdf


def _datos_gasto(data: CorreccionGastoIn, original: dict[str, Any]) -> dict[str, Any]:
    """Valores del ``hr.expense`` de corrección: nombre y tipo del original, el resto lo que corrigió el conductor."""
    product_id = related_id(original.get("product_id"))
    if product_id is None:
        raise SubmissionError("El gasto original no tiene tipo (producto) en Odoo", status_code=409)
    return {"name": _texto(original.get("name")), "product_id": product_id, "total_amount": float(data.total),
            "date": data.fecha.isoformat()}


def _datos_pos(data: CorreccionPosIn) -> dict[str, Any]:
    return {"km_start": float(data.km_inicial), "km_end": float(data.km_final),
            "check_in": _odoo_datetime(data.entrada), "check_out": _odoo_datetime(data.salida)}


def submit(db: Database, writer: OdooWriter, identity: TelegramIdentity, data: CorreccionGastoIn | CorreccionPosIn,
           diferido: TiemposDiferido | None = None) -> tuple[CorreccionRecibida, bool]:
    """Guarda la corrección como ``pendiente``. Devuelve (envío, era_duplicado). Idempotente por ``uuid``.

    ``diferido`` (ya validado dentro de la ventana) deja la trazabilidad sin señal. Una segunda corrección del mismo
    registro mientras la primera está en curso o enviada se rechaza (409).
    """
    with db.session() as session:
        existing = session.scalar(select(Correccion).where(Correccion.uuid == data.uuid))
        if existing is not None:
            if existing.telegram_user_id != identity.user_id:
                raise SubmissionError("Identificador de formulario en uso", status_code=409)
            return resumen_recibido(existing), True

    empleado = preop_service.employee_for_telegram(db, identity.user_id)
    if empleado is None:
        raise SubmissionError("Tu usuario de Telegram no está asociado a un empleado activo", 403)
    with db.session() as session:
        if data.odoo_id in _ids_en_curso(session, data.tipo):
            raise SubmissionError("Ya enviaste una corrección para este registro: está en proceso", status_code=409)

    original = _leer_original(writer, data, empleado)  # red fuera de la transacción
    archivo: bytes | None
    nombre: str | None
    if isinstance(data, CorreccionGastoIn):
        datos, explicacion, estado_original = _datos_gasto(data, original), data.descripcion, None
        archivo, nombre = _a_pdf(data.pdf), nombre_planilla(data.pdf_nombre, "pdf")
        referencia = _texto(original.get("name"))
    else:
        datos, explicacion, estado_original = _datos_pos(data), data.observaciones, _texto(original.get("state"))
        archivo = _a_pdf(data.planilla) if data.planilla else None
        nombre = nombre_planilla(data.planilla_nombre, "pdf") if data.planilla else None
        referencia = _texto(original.get("name"))

    with db.session() as session:
        fila = Correccion(
            uuid=data.uuid, telegram_user_id=identity.user_id, telegram_username=identity.username,
            firma_nombre=identity.full_name[:255] or empleado.nombre, empleado_id=empleado.id, tipo=data.tipo,
            odoo_id_original=data.odoo_id, referencia=referencia[:255] or None, estado_original=estado_original,
            fecha=utc_now(), datos=datos, explicacion=explicacion, adjunto=archivo, adjunto_nombre=nombre,
            adjunto_sha256=hashlib.sha256(archivo).hexdigest() if archivo else None,
            sync_status=SyncStatus.PENDIENTE, next_attempt_at=utc_now(),
        )
        session.add(fila)
        try:
            session.flush()
            if diferido is not None:
                session.add(CorreccionEnvioDiferido(
                    uuid=fila.uuid, telegram_user_id=identity.user_id, estado=EstadoDiferido.ACEPTADO,
                    correccion_id=fila.id, apertura_at=diferido.apertura, sin_senal_desde=diferido.sin_senal_desde,
                    recibido_at=diferido.recibido, retraso_segundos=diferido.retraso_segundos,
                    payload_sha256=payload_sha256(data),
                ))
            session.flush()
        except IntegrityError as exc:  # el mismo uuid llegó dos veces en paralelo
            raise SubmissionError("Formulario ya recibido", status_code=409) from exc
        return resumen_recibido(fila), False


# ──────────────────────────────────── envío a Odoo ────────────────────────────────────

def build_gasto_payload(c: Correccion) -> dict[str, Any]:
    """Cuerpo EXACTO de ``POST /api/gastos/register`` para la corrección de un gasto (sin otras claves)."""
    assert c.adjunto is not None
    d = c.datos
    return {
        "name": d["name"],
        "product_id": d["product_id"],
        "total_amount": float(d["total_amount"]),
        "employee_id": c.empleado.odoo_id,
        "description": c.explicacion,
        "date": d["date"],
        "state_expenses": cat.GASTO_CORRECCION,
        "type_file": "pdf",
        "attachment_filename": c.adjunto_nombre,
        "attachment": base64.b64encode(c.adjunto).decode("ascii"),
    }


def bloque_observacion(c: Correccion) -> str:
    """Texto que se agrega al final de las observaciones: ``[Corrección AAAA-MM-DD HH:MM] <explicación>``.

    La hora es la de recepción (Colombia), fija: así un reintento produce exactamente el mismo bloque.
    """
    return f"{cat.SEPARADOR_OBSERVACION.format(fecha=_hora_colombia(c.fecha).replace('T', ' '))}{c.explicacion}"


def observaciones_finales(actuales: str, bloque: str) -> str:
    """Observaciones actuales + separador + bloque nuevo (solo el bloque si no había nada). No lo duplica."""
    if bloque in actuales:
        return actuales
    return f"{actuales.rstrip()}{SEPARADOR_OBSERVACIONES}{bloque}" if actuales.strip() else bloque


def build_pos_values(c: Correccion) -> dict[str, Any]:
    """``vals`` del ``write`` sobre el ``pos.operacional``. Sin PDF nuevo no se toca la planilla actual.

    ``state`` y ``observations`` NO van aquí: ``send_pos`` los decide con lo que Odoo tiene en el momento de escribir
    (el estado y las observaciones actuales, a las que se agrega ``bloque_observacion`` al final).
    """
    vals: dict[str, Any] = {**c.datos}
    if c.adjunto:
        vals.update({"attachment": base64.b64encode(c.adjunto).decode("ascii"),  # Binary: base64 sin prefijo data:
                     "attachment_filename": c.adjunto_nombre, "type_file": "pdf"})
    return vals


@dataclass(frozen=True)
class CorreccionOutcome:
    correccion_id: int
    telegram_user_id: int
    tipo: str
    status: SyncStatus
    referencia: str | None = None
    odoo_id: int | None = None
    descartado: bool = False
    error: str | None = None


@dataclass(frozen=True)
class _Resultado:
    status: SyncStatus
    odoo_id: int | None = None
    error: str | None = None
    descartado: bool = False
    sin_cliente: bool = False  # falta configurar la API REST: no consume intentos


def _mismos_valores(actual: dict[str, Any], vals: dict[str, Any], bloque: str) -> bool:
    """El Pos ya corregido tiene los valores de esta corrección (la planilla no se compara: es binaria).

    Las observaciones no se comparan por igualdad (se agregan al final): basta con que contengan el bloque de esta
    corrección.
    """
    return (abs(float(actual.get("km_start") or 0) - vals["km_start"]) < 1e-6
            and abs(float(actual.get("km_end") or 0) - vals["km_end"]) < 1e-6
            and _texto(actual.get("check_in")) == vals["check_in"]
            and _texto(actual.get("check_out")) == vals["check_out"]
            and bloque in _texto(actual.get("observations")))


def _send_gasto(writer: OdooWriter, rest_client: OdooRestClient | None, c_id: int, payload: dict[str, Any],
                original_id: int, recibido: datetime, attempts: int, max_attempts: int) -> _Resultado:
    try:
        odoo_id = enviar_gasto(
            writer, rest_client, payload, c_id,
            # No la llave natural tal cual: coincidiría con el gasto rechazado original (ver el writer).
            buscar=lambda p: writer.buscar_gasto_corregido(p, [original_id], recibido),
            etiqueta=f"Corrección de gasto {original_id} (envío)")
    except RestNoConfigurado as exc:
        logger.error("Corrección %s en espera: %s", c_id, exc)
        return _Resultado(SyncStatus.ERROR, error=str(exc), sin_cliente=True)
    except OdooPermanentError as exc:
        return _Resultado(SyncStatus.FALLIDO, error=f"Odoo rechazó la corrección del gasto: {exc}")
    except OdooTransientError as exc:
        return _Resultado(SyncStatus.FALLIDO if attempts >= max_attempts else SyncStatus.ERROR,
                          error=f"Intento {attempts}/{max_attempts}: {exc}")
    return _Resultado(SyncStatus.ENVIADO, odoo_id=odoo_id)


def _send_pos(writer: OdooWriter, original_id: int, vals: dict[str, Any], bloque: str, attempts: int,
              max_attempts: int) -> _Resultado:
    """Relee el estado y las observaciones, descarta si ya no está pendiente y escribe sobre el MISMO registro.

    Idempotente: si Odoo ya está corregido con los mismos valores y el bloque de observaciones, no escribe.
    """
    try:
        actual = writer.leer_registro("pos.operacional", original_id, ["state", "km_start", "km_end", "check_in",
                                                                         "check_out", "observations"])
        if actual is None:
            return _Resultado(SyncStatus.FALLIDO, error="El Post-operacional ya no existe en Odoo", descartado=True)
        estado = _texto(actual.get("state"))
        if estado in cat.ESTADOS_POS_CORREGIDOS and _mismos_valores(actual, vals, bloque):
            logger.info("Corrección de Pos %s ya estaba aplicada en Odoo (%s): no se escribió de nuevo",
                        original_id, estado)
            return _Resultado(SyncStatus.ENVIADO, odoo_id=original_id)
        if estado not in cat.ESTADOS_POS_PENDIENTES:
            texto = cat.POS_ESTADO_TEXTO.get(estado, estado or "sin estado")
            return _Resultado(SyncStatus.FALLIDO, descartado=True,
                              error=f"El Post-operacional ya no estaba pendiente de corrección (estado en Odoo: {texto})")
        observaciones = observaciones_finales(_texto(actual.get("observations")), bloque)
        writer.escribir_pos_operacional(
            original_id, {**vals, "observations": observaciones, "state": cat.POS_ESTADO_CORREGIDO[estado]})
    except OdooPermanentError as exc:
        return _Resultado(SyncStatus.FALLIDO, error=f"Odoo rechazó la corrección del Post-operacional: {exc}")
    except OdooTransientError as exc:
        return _Resultado(SyncStatus.FALLIDO if attempts >= max_attempts else SyncStatus.ERROR,
                          error=f"Intento {attempts}/{max_attempts}: {exc}")
    return _Resultado(SyncStatus.ENVIADO, odoo_id=original_id)


def send_one(db: Database, writer: OdooWriter, correccion_id: int, max_attempts: int,
             rest_client: OdooRestClient | None = None) -> CorreccionOutcome:
    """Envía una corrección ya reclamada (``enviando``) y deja el estado final en la BD.

    Gasto: 4xx -> ``fallido``; 5xx o red -> ``error`` con backoff hasta ``max_attempts``. Pos: si el registro ya no
    está pendiente queda ``fallido`` con ``descartado`` (no se reencola) y se avisa al conductor.
    """
    with db.session() as session:
        c = session.get(Correccion, correccion_id)
        if c is None or c.sync_status != SyncStatus.ENVIANDO:
            raise LookupError(f"corrección {correccion_id} no está en estado enviando")
        tipo, original_id, recibido = c.tipo, c.odoo_id_original, c.fecha
        attempts, user_id, referencia = c.attempts, c.telegram_user_id, c.referencia
        if tipo == cat.TIPO_GASTO:
            payload = build_gasto_payload(c) if c.adjunto is not None else None
            vals: dict[str, Any] = {}
            bloque = ""
        else:
            payload, vals, bloque = None, build_pos_values(c), bloque_observacion(c)

    if tipo == cat.TIPO_GASTO:
        if payload is None:
            res = _Resultado(SyncStatus.FALLIDO, error="La corrección no tiene el PDF adjunto")
        else:
            res = _send_gasto(writer, rest_client, correccion_id, payload, original_id, recibido, attempts, max_attempts)
    else:
        res = _send_pos(writer, original_id, vals, bloque, attempts, max_attempts)

    now = utc_now()
    with db.session() as session:
        c = session.get(Correccion, correccion_id)
        assert c is not None
        c.sync_status, c.error_log, c.descartado = res.status, res.error, res.descartado
        if res.status == SyncStatus.ENVIADO:
            c.odoo_id, c.sent_at, c.next_attempt_at = res.odoo_id, now, None
        elif res.sin_cliente:
            # Es de configuración, no del registro: no consume intentos ni pasa a fallido.
            c.attempts = max(c.attempts - 1, 0)
            c.next_attempt_at = now + preop_service.backoff(1) * 10
        elif res.status == SyncStatus.ERROR:
            c.next_attempt_at = now + preop_service.backoff(attempts)
        else:
            c.next_attempt_at = None
    log = logger.info if res.status == SyncStatus.ENVIADO else logger.warning
    log("Corrección %s (%s %s) -> %s%s", correccion_id, tipo, original_id, res.status.value,
        f" ({res.error})" if res.error else f" odoo_id={res.odoo_id}")
    return CorreccionOutcome(correccion_id, user_id, tipo, res.status, referencia, res.odoo_id, res.descartado, res.error)


def claim_batch(db: Database, limit: int, lease_seconds: int, now: datetime | None = None) -> list[int]:
    return preop_service.claim_batch(db, limit, lease_seconds, now, model=Correccion)


def requeue_failed(db: Database, ids: list[int] | None = None) -> int:
    """Devuelve a ``pendiente`` las correcciones ``fallido``, salvo las descartadas (el registro ya no aplicaba)."""
    stmt = update(Correccion).where(Correccion.sync_status == SyncStatus.FALLIDO, Correccion.descartado.is_(False))
    if ids:
        stmt = stmt.where(Correccion.id.in_(ids))
    with db.session() as session:
        result = session.execute(
            # attempts=1: el siguiente envío busca antes de postear (un fallido por timeout pudo crearse en Odoo).
            stmt.values(sync_status=SyncStatus.PENDIENTE, attempts=1, next_attempt_at=utc_now(), error_log=None)
            .execution_options(synchronize_session=False))
        return int(result.rowcount or 0)  # type: ignore[attr-defined]  # CursorResult

