"""Restricción de acceso de la Prueba de Ruta por acuerdo (``agreement``) del **extracto**.

Regla del usuario (2026-11-25, revisada el mismo día): solo pueden hacer la Prueba de Ruta los
conductores con un **extracto de contrato aprobado y vigente** (``car.extractos`` en estado
"Aprobado", el mismo criterio del preoperacional: ``preop.service.extractos_del_conductor``)
cuyo ``contratos_list`` incluye uno de los acuerdos de ``PRUEBA_RUTA_ACUERDOS``. Si el
conductor ya tiene el vehículo reservado por su preoperacional, cuenta **solo el extracto de
esa reserva** (el del vehículo que va a probar). El contrato de la ficha del empleado
(``id_contract`` / contrato temporal) ya **no** autoriza. Los demás ven "Contrato No Autorizado".

Los nombres se comparan normalizados: sin mayúsculas, tildes, puntuación ni el prefijo
``[código]`` con que la API de acuerdos antepone el código (``[New] MINERA ...``). Se acepta el
contrato si coincide su **nombre** normalizado o si su **id** es el de un acuerdo sincronizado
(tabla ``acuerdos``) cuyo nombre normalizado está en la lista; así no depende de cómo escriba
el nombre cada endpoint.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..preop.models import Acuerdo, Empleado
from ..preop.service import extractos_del_conductor

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
    extracto_id: int | None = None      # id local del extracto aprobado que autorizó
    extracto_codigo: str | None = None


def contratos_extractos(session: Session, empleado: Empleado, extracto_id: int | None = None) -> list[str]:
    """Nombres de los contratos de sus extractos aprobados y vigentes (para el mensaje de bloqueo)."""
    extractos = [e for e in extractos_del_conductor(session, empleado)
                 if extracto_id is None or e.id == extracto_id]
    nombres = [str(c.get("nombre") or "").strip() for e in extractos for c in e.contratos_list]
    return list(dict.fromkeys(n for n in nombres if n))


def contrato_autorizado(session: Session, empleado: Empleado, autorizados: frozenset[str],
                        extracto_id: int | None = None) -> ContratoAutorizado | None:
    """Primer contrato autorizado de un extracto **aprobado y vigente** del conductor; None si ninguno.

    ``extracto_id`` (el de la reserva del vehículo) limita la búsqueda a ese extracto.
    """
    if not autorizados:
        return None
    extractos = [e for e in extractos_del_conductor(session, empleado)
                 if extracto_id is None or e.id == extracto_id]
    candidatos = [(e, c.get("id_contrato"), str(c.get("nombre") or ""))
                  for e in extractos for c in e.contratos_list if c.get("id_contrato") or c.get("nombre")]
    ids = [cid for _, cid, _ in candidatos if cid]
    nombres_por_id = {a.odoo_id: a.nombre for a in session.scalars(
        select(Acuerdo).where(Acuerdo.odoo_id.in_(ids)))} if ids else {}
    for extracto, cid, nombre in candidatos:
        acuerdo = nombres_por_id.get(cid) if cid else None
        if normalizar_acuerdo(nombre) in autorizados or normalizar_acuerdo(acuerdo) in autorizados:
            return ContratoAutorizado(cid, nombre or _PREFIJO_CODIGO.sub("", acuerdo or ""),
                                      extracto.id, extracto.codigo)
    return None


def acuerdos_sin_sincronizar(session: Session, autorizados: frozenset[str]) -> list[str]:
    """Nombres de la lista que no corresponden a ningún acuerdo sincronizado (para el log al arrancar)."""
    existentes = {normalizar_acuerdo(a) for a in session.scalars(select(Acuerdo.nombre))}
    return sorted(n for n in autorizados if n not in existentes)
