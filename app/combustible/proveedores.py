"""Proveedores de combustible aprobados: lista local que carga el negocio desde un CSV.

Regla del usuario (2026-11-25): la Mini App ofrece la lista de proveedores aprobados; si el
proveedor no está, el conductor lo escribe a mano y va a Odoo como texto (``supplier``).
Un proveedor de la lista con contacto de Odoo (``odoo_partner_id``) va en ``proveedor``
(``res.partner``); sin contacto, su nombre va también en ``supplier``.

CSV (UTF-8, separado por ``;`` o ``,``, con encabezado): ``nombre`` (obligatorio), ``nit`` y
``odoo_partner_id`` (opcionales). Se actualiza por NIT o, sin NIT, por nombre normalizado::

    python -m app.combustible.proveedores cargar proveedores.csv [--resolver-odoo] [--reemplazar]
    python -m app.combustible.proveedores listar
    python -m app.combustible.proveedores desactivar 3 7

``--resolver-odoo`` busca en Odoo (solo lectura) el contacto de las filas sin
``odoo_partner_id``: por NIT y, si no, por nombre exacto; solo si el resultado es único.
``--reemplazar`` desactiva los proveedores que no vengan en el archivo.
"""
from __future__ import annotations

import argparse
import csv
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..preop.db import Database
from ..preop.models import ProveedorCombustible
from ..prueba_ruta.acuerdos import normalizar_acuerdo as normalizar_nombre

# (nit, nombre) -> id del contacto de Odoo o None
ResolverContacto = Callable[[str | None, str], "int | None"]


@dataclass(frozen=True)
class FilaProveedor:
    nombre: str
    nit: str | None = None
    odoo_partner_id: int | None = None


@dataclass(frozen=True)
class ResumenCarga:
    nuevos: int
    actualizados: int
    desactivados: int
    sin_contacto: tuple[str, ...]  # proveedores sin contacto de Odoo (irán como texto)


def normalizar_nit(nit: str | None) -> str | None:
    """'900.123.456-7' -> '9001234567'. Vacío -> None."""
    digitos = re.sub(r"\D", "", nit or "")
    return digitos or None


def leer_csv(texto: str) -> list[FilaProveedor]:
    """Filas del CSV. Lanza ValueError con la línea si un dato es inválido."""
    texto = texto.lstrip("﻿")
    muestra = texto[:2048]
    delimitador = ";" if muestra.count(";") >= muestra.count(",") else ","
    lector = csv.DictReader(io.StringIO(texto), delimiter=delimitador)
    columnas = {(c or "").strip().lower() for c in (lector.fieldnames or [])}
    if "nombre" not in columnas:
        raise ValueError("El CSV debe tener la columna 'nombre' (opcionales: 'nit', 'odoo_partner_id')")
    filas: list[FilaProveedor] = []
    for linea, raw in enumerate(lector, start=2):
        fila = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        nombre = " ".join(fila.get("nombre", "").split())
        if not nombre:
            continue
        partner = fila.get("odoo_partner_id") or ""
        if partner and not partner.isdigit():
            raise ValueError(f"Línea {linea}: odoo_partner_id debe ser un número ({partner!r})")
        filas.append(FilaProveedor(nombre[:255], normalizar_nit(fila.get("nit")), int(partner) if partner else None))
    return filas


def listar_activos(session: Session) -> list[ProveedorCombustible]:
    return list(session.scalars(
        select(ProveedorCombustible).where(ProveedorCombustible.activo.is_(True)).order_by(ProveedorCombustible.nombre)
    ))


def resolver_contactos(filas: Iterable[FilaProveedor], resolver: ResolverContacto) -> list[FilaProveedor]:
    """Completa ``odoo_partner_id`` de las filas que no lo traen (fuera de cualquier transacción)."""
    out = []
    for fila in filas:
        if fila.odoo_partner_id is None:
            fila = FilaProveedor(fila.nombre, fila.nit, resolver(fila.nit, fila.nombre))
        out.append(fila)
    return out


def cargar(db: Database, filas: list[FilaProveedor], reemplazar: bool = False) -> ResumenCarga:
    """UPSERT por NIT o por nombre normalizado. Reactiva los que vuelven a venir en el archivo."""
    nuevos = actualizados = desactivados = 0
    vistos: set[int] = set()
    with db.session() as session:
        for fila in filas:
            clave = normalizar_nombre(fila.nombre)
            if not clave:
                continue
            condiciones = [ProveedorCombustible.clave == clave]
            if fila.nit:
                condiciones.append(ProveedorCombustible.nit == fila.nit)
            existentes = list(session.scalars(select(ProveedorCombustible).where(or_(*condiciones))))
            # El NIT manda: si hay uno con ese NIT, es ese aunque el nombre haya cambiado.
            prov = next((p for p in existentes if fila.nit and p.nit == fila.nit), existentes[0] if existentes else None)
            if prov is None:
                prov = ProveedorCombustible(nombre=fila.nombre, clave=clave, nit=fila.nit,
                                            odoo_partner_id=fila.odoo_partner_id, activo=True)
                session.add(prov)
                nuevos += 1
            else:
                prov.nombre, prov.clave, prov.activo = fila.nombre, clave, True
                prov.nit = fila.nit or prov.nit
                prov.odoo_partner_id = fila.odoo_partner_id or prov.odoo_partner_id
                actualizados += 1
            session.flush()
            vistos.add(prov.id)
        if reemplazar:
            for prov in listar_activos(session):
                if prov.id not in vistos:
                    prov.activo = False
                    desactivados += 1
        sin_contacto = tuple(p.nombre for p in listar_activos(session) if not p.odoo_partner_id)
    return ResumenCarga(nuevos, actualizados, desactivados, sin_contacto)


def desactivar(db: Database, ids: list[int]) -> int:
    with db.session() as session:
        provs = list(session.scalars(select(ProveedorCombustible).where(ProveedorCombustible.id.in_(ids))))
        for prov in provs:
            prov.activo = False
        return len(provs)


def main() -> None:  # pragma: no cover - CLI de operación
    from ..config import get_settings
    from ..preop.worker import build_writer

    parser = argparse.ArgumentParser(description="Proveedores de combustible aprobados")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_cargar = sub.add_parser("cargar", help="carga o actualiza proveedores desde un CSV")
    p_cargar.add_argument("archivo", type=Path)
    p_cargar.add_argument("--resolver-odoo", action="store_true", help="busca el contacto en Odoo por NIT o nombre")
    p_cargar.add_argument("--reemplazar", action="store_true", help="desactiva los que no estén en el archivo")
    sub.add_parser("listar", help="lista los proveedores activos")
    p_des = sub.add_parser("desactivar", help="desactiva proveedores por id")
    p_des.add_argument("ids", type=int, nargs="+")
    args = parser.parse_args()

    settings = get_settings()
    db = Database(settings.database_url)
    if args.cmd == "listar":
        with db.session() as session:
            for p in listar_activos(session):
                print(f"{p.id:>4}  {p.nombre}  NIT={p.nit or '—'}  contacto_odoo={p.odoo_partner_id or '—'}")
        return
    if args.cmd == "desactivar":
        print(f"Desactivados: {desactivar(db, args.ids)}")
        return
    filas = leer_csv(args.archivo.read_text(encoding="utf-8-sig"))
    if args.resolver_odoo:
        filas = resolver_contactos(filas, build_writer(settings).buscar_contacto)
    resumen = cargar(db, filas, reemplazar=args.reemplazar)
    print(f"Nuevos: {resumen.nuevos}  Actualizados: {resumen.actualizados}  Desactivados: {resumen.desactivados}")
    if resumen.sin_contacto:
        print("Sin contacto de Odoo (irán como texto en 'supplier'):", "; ".join(resumen.sin_contacto))


if __name__ == "__main__":  # pragma: no cover
    main()
