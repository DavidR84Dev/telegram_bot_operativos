"""Registro de conductores nuevos (opción 📝 Registro del bot).

Flujo (lo dispara la conversación de ``telegram_app``):

1. El conductor escribe nombre completo, número de documento y correo. Los datos y su
   id de Telegram se guardan en ``registros_conductor`` (estado ``pendiente``).
2. Se busca el empleado en ``/api/empleados`` por número de documento
   (``identificacion``, comparando solo dígitos) en las compañías de
   ``ODOO_API_COMPANY_IDS``.
3. Si no existe -> ``no_encontrado``. Si su PIN ya es el id de **otro** usuario de
   Telegram -> ``pin_ocupado`` (no se sobrescribe). Si no -> ``POST
   /api/empleados/register`` ``{id, pin}`` fija ``hr.employee.pin`` = id de Telegram,
   se actualiza la copia local de ``empleados`` (para que el preoperacional lo
   reconozca sin esperar la sincronización) y el registro queda ``registrado``.

Las llamadas a Odoo se hacen fuera de las transacciones de la BD local.
``/api/empleados/register`` **crea** un empleado si el ``id`` no existe; por eso solo
se llama con el ``id`` de un empleado devuelto por ``/api/empleados``.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from sqlalchemy import select, update

from .db import Database
from .models import Empleado, RegistroConductor, utc_now
from .rest_client import OdooRestClient

logger = logging.getLogger(__name__)

REGISTRADO = "registrado"
NO_ENCONTRADO = "no_encontrado"
PIN_OCUPADO = "pin_ocupado"
ERROR = "error"
PENDIENTE = "pendiente"

_NOMBRE_RE = re.compile(r"^[^\W\d_]+(?:[ '\-.][^\W\d_]+)*\.?$")
_CORREO_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


# ─────────────────────────────────────── validaciones ───────────────────────────────────────

def limpiar_nombre(texto: str) -> str | None:
    """Nombre completo: al menos nombre y apellido, solo letras. None si no es válido."""
    nombre = " ".join(texto.split())
    if not 5 <= len(nombre) <= 120 or len(nombre.split(" ")) < 2 or not _NOMBRE_RE.match(nombre):
        return None
    return nombre


def limpiar_documento(texto: str) -> str | None:
    """Documento sin puntos, espacios ni guiones; entre 5 y 15 dígitos. None si no es válido."""
    documento = re.sub(r"[\s.\-]", "", texto)
    if not documento.isdigit() or not 5 <= len(documento) <= 15:
        return None
    return documento


def limpiar_correo(texto: str) -> str | None:
    correo = texto.strip().lower()
    if len(correo) > 254 or not _CORREO_RE.match(correo):
        return None
    return correo


def _solo_digitos(valor: Any) -> str:
    return "".join(ch for ch in str(valor or "") if ch.isdigit())


def _texto(valor: Any) -> str:
    texto = str(valor or "").strip()
    return "" if texto.lower() in ("false", "none") else texto


def _sin_tildes(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


# ─────────────────────────────────────────── BD ───────────────────────────────────────────

def ya_registrado(db: Database, telegram_user_id: int) -> bool:
    """True si el usuario completó el registro (ya no tiene acceso a la opción 📝 Registro)."""
    with db.session() as session:
        estado = session.scalar(
            select(RegistroConductor.estado).where(RegistroConductor.telegram_user_id == telegram_user_id)
        )
    return estado == REGISTRADO


def _guardar(db: Database, telegram_user_id: int, **valores: Any) -> None:
    with db.session() as session:
        fila = session.scalar(
            select(RegistroConductor).where(RegistroConductor.telegram_user_id == telegram_user_id)
        )
        if fila is None:
            fila = RegistroConductor(telegram_user_id=telegram_user_id, intentos=0)
            session.add(fila)
        for campo, valor in valores.items():
            setattr(fila, campo, valor)


# ───────────────────────────────────────── proceso ─────────────────────────────────────────

@dataclass(frozen=True)
class DatosRegistro:
    telegram_user_id: int
    telegram_username: str | None
    nombre_completo: str
    documento: str
    correo: str


@dataclass(frozen=True)
class ResultadoRegistro:
    estado: str
    empleado_nombre: str | None = None


def buscar_empleado(client: OdooRestClient, company_ids: Sequence[int], documento: str) -> dict[str, Any] | None:
    """Empleado de ``/api/empleados`` cuyo número de identificación coincide (solo dígitos)."""
    for company_id in company_ids:
        for empleado in client.iter_items("/api/empleados", company_id):
            if _solo_digitos(empleado.get("identificacion")) == documento:
                return empleado
    return None


def registrar_conductor(
    db: Database,
    client_factory: Callable[[], OdooRestClient],
    company_ids: Sequence[int],
    datos: DatosRegistro,
) -> ResultadoRegistro:
    """Guarda los datos, valida al conductor en Odoo y registra su id de Telegram como PIN."""
    uid = datos.telegram_user_id
    with db.session() as session:
        fila = session.scalar(select(RegistroConductor).where(RegistroConductor.telegram_user_id == uid))
        intentos = (fila.intentos if fila else 0) + 1
    _guardar(db, uid, telegram_username=datos.telegram_username, nombre_completo=datos.nombre_completo,
             documento=datos.documento, correo=datos.correo, estado=PENDIENTE, intentos=intentos,
             error_log=None, empleado_odoo_id=None)

    try:
        with client_factory() as client:
            empleado = buscar_empleado(client, company_ids, datos.documento)
            if empleado is None:
                _guardar(db, uid, estado=NO_ENCONTRADO)
                logger.info("Registro: documento de %s no existe en Odoo", uid)
                return ResultadoRegistro(NO_ENCONTRADO)

            odoo_id = int(empleado["id"])
            nombre = _texto(empleado.get("nombre")) or None
            pin_actual = _texto(empleado.get("codigo_pin"))
            if pin_actual and pin_actual != str(uid):
                _guardar(db, uid, estado=PIN_OCUPADO, empleado_odoo_id=odoo_id,
                         error_log=f"El empleado {odoo_id} ya tiene otro id de Telegram en el PIN")
                logger.warning("Registro: el empleado %s ya tiene el PIN de otro usuario (intento de %s)",
                               odoo_id, uid)
                return ResultadoRegistro(PIN_OCUPADO, nombre)

            if pin_actual != str(uid):
                client.post_json("/api/empleados/register", {"id": odoo_id, "pin": str(uid)})
    except Exception as exc:  # red, API caída, credenciales: el conductor puede reintentar
        _guardar(db, uid, estado=ERROR, error_log=f"{type(exc).__name__}: {exc}"[:2000])
        logger.exception("Registro de %s: no se pudo validar en Odoo", uid)
        return ResultadoRegistro(ERROR)

    with db.session() as session:
        # Copia local de empleados: el preoperacional reconoce al conductor sin esperar la sincronización.
        session.execute(update(Empleado).where(Empleado.odoo_id == odoo_id).values(codigo_pin=str(uid)))
    _guardar(db, uid, estado=REGISTRADO, empleado_odoo_id=odoo_id, registrado_at=utc_now())
    if nombre and _sin_tildes(nombre).casefold() != _sin_tildes(datos.nombre_completo).casefold():
        logger.info("Registro de %s: nombre escrito distinto al de Odoo (empleado %s)", uid, odoo_id)
    logger.info("Registro: usuario %s asociado al empleado %s", uid, odoo_id)
    return ResultadoRegistro(REGISTRADO, nombre)
