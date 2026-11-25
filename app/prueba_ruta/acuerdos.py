"""Restricción de acceso de la Prueba de Ruta por acuerdo (``agreement``) del conductor.

Regla del usuario (2026-11-25): solo pueden hacer la Prueba de Ruta los conductores cuyo
**contrato del empleado** (``id_contract`` o el contrato temporal, ambos ``agreement`` en
Odoo) es uno de los acuerdos de ``PRUEBA_RUTA_ACUERDOS``. Los demás ven "Contrato No
Autorizado". Los nombres se comparan normalizados: sin mayúsculas, tildes, puntuación ni
el prefijo ``[código]`` con que la API de acuerdos antepone el código (``[New] MINERA ...``).

Se acepta el contrato si coincide su **nombre** normalizado o si su **id** es el de un
acuerdo sincronizado (tabla ``acuerdos``) cuyo nombre normalizado está en la lista; así no
depende de cómo escriba el nombre cada endpoint.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..preop.models import Acuerdo, Empleado

CONTRATO_NO_AUTORIZADO = "Contrato No Autorizado"

_PREFIJO_CODIGO = re.compile(r"^\s*\[[^\]]*\]\s*")


def normalizar_acuerdo(nombre: str | None) -> str:
    """'[New] Minera de Cobre  Quebradoná' -> 'MINERA DE COBRE QUEBRADONA'; 'S.A.S.' -> 'SAS'."""
    texto = _PREFIJO_CODIGO.sub("", nombre or "")
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
    # Los puntos de siglas no separan ("S.A.S." = "SAS"); el resto de la puntuación es un espacio.
    texto = re.sub(r"[^0-9A-Za-z]+", " ", texto.replace(".", ""))
    return " ".join(texto.upper().split())


def normalizar_lista(nombres: Iterable[str]) -> frozenset[str]:
    return frozenset(n for n in (normalizar_acuerdo(x) for x in nombres) if n)


@dataclass(frozen=True)
class ContratoAutorizado:
    odoo_id: int | None
    nombre: str


def contrato_autorizado(session: Session, empleado: Empleado, autorizados: frozenset[str]) -> ContratoAutorizado | None:
    """Primer contrato del empleado (fijo o temporal) que es un acuerdo autorizado; None si ninguno."""
    if not autorizados:
        return None
    candidatos = [(cid, nombre) for cid, nombre in ((empleado.id_contract, empleado.contrato),
                                                    (empleado.contrato_temporal_id, empleado.contrato_temporal))
                  if cid or nombre]
    ids = [cid for cid, _ in candidatos if cid]
    nombres_por_id = {a.odoo_id: a.nombre for a in session.scalars(
        select(Acuerdo).where(Acuerdo.odoo_id.in_(ids)))} if ids else {}
    for cid, nombre in candidatos:
        acuerdo = nombres_por_id.get(cid) if cid else None
        if normalizar_acuerdo(nombre) in autorizados or normalizar_acuerdo(acuerdo) in autorizados:
            return ContratoAutorizado(cid, nombre or _PREFIJO_CODIGO.sub("", acuerdo or ""))
    return None


def acuerdos_sin_sincronizar(session: Session, autorizados: frozenset[str]) -> list[str]:
    """Nombres de la lista que no corresponden a ningún acuerdo sincronizado (para el log al arrancar)."""
    existentes = {normalizar_acuerdo(a) for a in session.scalars(select(Acuerdo.nombre))}
    return sorted(n for n in autorizados if n not in existentes)
