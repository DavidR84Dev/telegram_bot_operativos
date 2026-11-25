"""Lógica del AST: contexto de la Mini App, recepción y envío a Odoo.

Reutiliza las reglas del preoperacional (``app.preop.service``): el empleado se
identifica por su PIN (= id de Telegram), el contrato sale de sus extractos
**aprobados y vigentes** y el jefe inmediato se resuelve por el nombre del
responsable. La cola (``claim_batch``/``requeue_failed``) es la misma, sobre la
tabla ``ast_trabajo``.

Todo es síncrono (SQLAlchemy + XML-RPC); el código async lo llama con ``asyncio.to_thread``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..preop import service as preop_service
from ..preop.db import Database
from ..preop.diferido import motivo_vencido
from ..preop.models import (
    AnalisisSeguro,
    AstEnvioDiferido,
    Empleado,
    EstadoDiferido,
    Extracto,
    SyncStatus,
    utc_now,
)
from ..preop.odoo_writer import OdooPermanentError, OdooTransientError, OdooWriter
from ..preop.service import (
    EnvioVencidoError,
    SubmissionError,
    TelegramIdentity,
    TiemposDiferido,
    payload_sha256,
)
from .catalogo import MEDICAMENTOS, PREGUNTAS_BY_KEY, SI, con_alerta
from .schemas import AstIn

logger = logging.getLogger(__name__)


# ───────────────────────────── consultas para la Mini App ─────────────────────────────

def _contratos(extractos: list[Extracto], empleado: Empleado) -> list[dict[str, Any]]:
    """Unión de los contratos de los extractos aprobados (sin repetir), con el extracto de cada uno.

    El AST no lleva vehículo: el extracto solo sirve para validar el contrato y como
    trazabilidad local (el modelo ``ast`` de Odoo no tiene campo de extracto).
    """
    vistos: dict[int, dict[str, Any]] = {}
    for extracto in extractos:
        for contrato in preop_service._contratos_permitidos(extracto, empleado):
            cid = int(contrato["id_contrato"])
            vistos.setdefault(cid, {"id_contrato": cid, "nombre": contrato.get("nombre") or f"Contrato {cid}",
                                    "extracto_id": extracto.id})
    return list(vistos.values())


def form_context(db: Database, empleado: Empleado, now: datetime | None = None) -> dict[str, Any]:
    """Información del personal: empleado, responsable y contratos de sus extractos aprobados."""
    with db.session() as session:
        contratos = _contratos(preop_service.extractos_del_conductor(session, empleado, now), empleado)
    return {
        "empleado": {"nombre": empleado.nombre, "puesto_trabajo": empleado.puesto_trabajo,
                     "responsable": empleado.responsable},
        "validado": bool(contratos),
        "contratos": [{"id_contrato": c["id_contrato"], "nombre": c["nombre"]} for c in contratos],
    }


# ───────────────────────────────────── recepción ──────────────────────────────────────

def submit_diferido(db: Database, identity: TelegramIdentity, data: AstIn,
                    tiempos: TiemposDiferido) -> tuple[AnalisisSeguro, bool]:
    """Recibe un AST guardado en el teléfono por falta de señal (regla de ``app/preop/diferido.py``).

    Dentro de la ventana sigue el flujo normal (mismas validaciones, Odoo y avisos) y deja su
    trazabilidad. Fuera de ella lo guarda solo como auditoría y lanza ``EnvioVencidoError``:
    no va a Odoo y no se notifica al conductor ni al jefe.
    """
    with db.session() as session:
        # El primer intento pudo llegar aunque el teléfono no recibió la respuesta: es el mismo formulario.
        existing = session.scalar(select(AnalisisSeguro).where(AnalisisSeguro.uuid == data.uuid))
        if existing is not None:
            if existing.telegram_user_id != identity.user_id:
                raise SubmissionError("Identificador de formulario en uso", status_code=409)
            return existing, True
        previo = session.scalar(select(AstEnvioDiferido).where(AstEnvioDiferido.uuid == data.uuid))
        if previo is not None:
            if previo.telegram_user_id != identity.user_id:
                raise SubmissionError("Identificador de formulario en uso", status_code=409)
            raise EnvioVencidoError(previo.motivo or "Envío vencido")

    motivo = motivo_vencido(tiempos.apertura, tiempos.sin_senal_desde, tiempos.recibido)
    if motivo is None:
        return submit(db, identity, data, diferido=tiempos)

    with db.session() as session:
        session.add(AstEnvioDiferido(
            uuid=data.uuid, telegram_user_id=identity.user_id, estado=EstadoDiferido.VENCIDO, motivo=motivo,
            apertura_at=tiempos.apertura, sin_senal_desde=tiempos.sin_senal_desde, recibido_at=tiempos.recibido,
            retraso_segundos=tiempos.retraso_segundos, payload_sha256=payload_sha256(data),
            payload=data.model_dump(mode="json", exclude={"diferido"}),
        ))
        try:
            session.flush()
        except IntegrityError as exc:  # el mismo uuid llegó dos veces en paralelo
            raise EnvioVencidoError(motivo) from exc
    logger.warning("AST diferido VENCIDO uuid=%s usuario=%s: %s", data.uuid, identity.user_id, motivo)
    raise EnvioVencidoError(motivo)


def submit(db: Database, identity: TelegramIdentity, data: AstIn,
           diferido: TiemposDiferido | None = None) -> tuple[AnalisisSeguro, bool]:
    """Guarda el AST como ``pendiente``. Devuelve (registro, era_duplicado).

    Idempotente por ``uuid``: un reenvío (doble toque, red inestable) devuelve el original.
    ``diferido`` (ya validado dentro de la ventana) deja la trazabilidad del envío sin señal.
    """
    with db.session() as session:
        existing = session.scalar(select(AnalisisSeguro).where(AnalisisSeguro.uuid == data.uuid))
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

        contratos = _contratos(preop_service.extractos_del_conductor(session, empleado), empleado)
        if not contratos:
            raise SubmissionError("No tienes un extracto aprobado y vigente como conductor", 403)
        por_id = {c["id_contrato"]: c for c in contratos}
        if data.contrato_odoo_id is not None:
            contrato = por_id.get(data.contrato_odoo_id)
            if contrato is None:
                raise SubmissionError("El contrato no corresponde a tus extractos aprobados")
        elif len(contratos) == 1:
            contrato = contratos[0]
        else:
            raise SubmissionError("Selecciona el contrato")

        respuestas = dict(data.respuestas)
        ast = AnalisisSeguro(
            uuid=data.uuid,
            telegram_user_id=identity.user_id,
            telegram_username=identity.username,
            firma_nombre=identity.full_name[:255] or empleado.nombre,
            empleado_id=empleado.id,
            extracto_id=contrato["extracto_id"],
            contrato_odoo_id=contrato["id_contrato"],
            fecha=utc_now(),
            respuestas=respuestas,
            nombre_medicamento=data.nombre_medicamento,
            observaciones=data.observaciones,
            no_conformes=len(con_alerta(respuestas)),
            sync_status=SyncStatus.PENDIENTE,
            next_attempt_at=utc_now(),
        )
        session.add(ast)
        try:
            session.flush()
            if diferido is not None:
                session.add(AstEnvioDiferido(
                    uuid=ast.uuid, telegram_user_id=identity.user_id, estado=EstadoDiferido.ACEPTADO,
                    ast_id=ast.id, apertura_at=diferido.apertura, sin_senal_desde=diferido.sin_senal_desde,
                    recibido_at=diferido.recibido, retraso_segundos=diferido.retraso_segundos,
                    payload_sha256=payload_sha256(data),
                ))
                session.flush()
        except IntegrityError as exc:  # el mismo uuid llegó dos veces en paralelo
            raise SubmissionError("Formulario ya recibido", status_code=409) from exc
        return ast, False


# ──────────────────────────────────── envío a Odoo ────────────────────────────────────

def build_odoo_values(ast: AnalisisSeguro, boss_odoo_id: int | None = None) -> dict[str, Any]:
    """Traduce el registro local a ``vals`` del modelo ``ast`` de Odoo.

    Solo se envían preguntas del catálogo. ``state`` y ``plan_accion`` los calcula Odoo
    ("No conforme" o medicamentos = "Sí" -> no conforme con plan de acción).
    """
    vals: dict[str, Any] = {
        "employee_id": ast.empleado.odoo_id,
        "company_id": ast.empleado.company_odoo_id,
        "observaciones": ast.observaciones or "",
    }
    if ast.contrato_odoo_id:
        vals["contrato_id"] = ast.contrato_odoo_id
    if boss_odoo_id:
        vals["immediate_boss_id"] = boss_odoo_id
    vals.update({key: value for key, value in ast.respuestas.items() if key in PREGUNTAS_BY_KEY})
    vals["nombre_medicamento"] = (ast.nombre_medicamento or "") if ast.respuestas.get(MEDICAMENTOS) == SI else ""
    return vals


@dataclass(frozen=True)
class AvisoJefeAst:
    """Aviso por Telegram al jefe inmediato cuando el AST trae respuestas con alerta."""

    telegram_id: int
    conductor: str
    contrato: str | None
    alertas: tuple[str, ...]
    medicamento: str | None


@dataclass(frozen=True)
class AstOutcome:
    ast_id: int
    telegram_user_id: int
    status: SyncStatus
    odoo_name: str | None = None
    no_conformes: int = 0
    error: str | None = None
    aviso_jefe: AvisoJefeAst | None = None


def _nombre_contrato(ast: AnalisisSeguro) -> str | None:
    extracto = ast.extracto
    if extracto is not None:
        for contrato in extracto.contratos_list:
            if contrato.get("id_contrato") == ast.contrato_odoo_id:
                return contrato.get("nombre")
    empleado = ast.empleado
    for cid, nombre in ((empleado.id_contract, empleado.contrato),
                        (empleado.contrato_temporal_id, empleado.contrato_temporal)):
        if cid and cid == ast.contrato_odoo_id:
            return nombre
    return None


def _aviso_jefe(session: Any, ast: AnalisisSeguro) -> AvisoJefeAst | None:
    """Aviso al jefe si hay "No conforme" o medicamentos = "Sí" (requiere su id de Telegram en el PIN)."""
    alertas = con_alerta(ast.respuestas)
    if not alertas:
        return None
    jefe = preop_service.jefe_inmediato(session, ast.empleado)
    pin = jefe.codigo_pin if jefe else None
    if not pin or not pin.isdigit():
        logger.warning("AST %s con alertas: el jefe (%s) no tiene id de Telegram en el PIN",
                       ast.id, ast.empleado.responsable or "sin responsable")
        return None
    return AvisoJefeAst(int(pin), ast.empleado.nombre, _nombre_contrato(ast),
                        tuple(p.corto for p in alertas), ast.nombre_medicamento)


def send_one(db: Database, writer: OdooWriter, ast_id: int, max_attempts: int) -> AstOutcome:
    """Envía un AST ya reclamado (``enviando``) y deja el estado final en la BD."""
    with db.session() as session:
        ast = session.get(AnalisisSeguro, ast_id)
        if ast is None or ast.sync_status != SyncStatus.ENVIANDO:
            raise LookupError(f"AST {ast_id} no está en estado enviando")
        jefe = preop_service.jefe_inmediato(session, ast.empleado)
        vals = build_odoo_values(ast, jefe.odoo_id if jefe else None)
        ref, submitted_at, attempts = ast.uuid, ast.fecha, ast.attempts
        aviso = _aviso_jefe(session, ast)
        user_id, no_conformes = ast.telegram_user_id, ast.no_conformes

    status: SyncStatus
    error: str | None = None
    odoo_id: int | None = None
    odoo_name: str | None = None
    try:
        # Red fuera de la transacción: no se retiene una conexión de BD durante el XML-RPC.
        odoo_id, odoo_name, reused = writer.create_ast(ref, vals, submitted_at, attempts > 1)
        status = SyncStatus.ENVIADO
        if reused:
            logger.info("AST %s ya existía en Odoo (id=%s): no se duplicó", ast_id, odoo_id)
    except OdooPermanentError as exc:
        status, error = SyncStatus.FALLIDO, f"Odoo rechazó el registro: {exc}"
    except OdooTransientError as exc:
        status = SyncStatus.FALLIDO if attempts >= max_attempts else SyncStatus.ERROR
        error = f"Intento {attempts}/{max_attempts}: {exc}"

    now = utc_now()
    with db.session() as session:
        ast = session.get(AnalisisSeguro, ast_id)
        assert ast is not None
        ast.sync_status = status
        ast.error_log = error
        if status == SyncStatus.ENVIADO:
            ast.odoo_id, ast.odoo_name, ast.sent_at = odoo_id, odoo_name, now
            ast.next_attempt_at = None
        elif status == SyncStatus.ERROR:
            ast.next_attempt_at = now + preop_service.backoff(attempts)
        else:
            ast.next_attempt_at = None
    log = logger.info if status == SyncStatus.ENVIADO else logger.warning
    log("AST %s -> %s%s", ast_id, status.value, f" ({error})" if error else f" odoo_id={odoo_id}")
    return AstOutcome(ast_id, user_id, status, odoo_name, no_conformes, error,
                      aviso if status == SyncStatus.ENVIADO else None)


def claim_batch(db: Database, limit: int, lease_seconds: int, now: datetime | None = None) -> list[int]:
    return preop_service.claim_batch(db, limit, lease_seconds, now, model=AnalisisSeguro)


def requeue_failed(db: Database, ast_ids: list[int] | None = None) -> int:
    return preop_service.requeue_failed(db, ast_ids, model=AnalisisSeguro)
