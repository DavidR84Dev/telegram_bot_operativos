"""Proveedores de combustible aprobados: lista local que carga el negocio desde un CSV.

Regla del usuario (2026-11-25): la Mini App ofrece la lista de proveedores aprobados; si el
proveedor no está, el conductor lo escribe a mano y va a Odoo como texto (``supplier``).
Un proveedor de la lista con contacto de Odoo (``odoo_partner_id``) va en ``proveedor``
(``res.partner``); sin contacto, su nombre va también en ``supplier``.

CSV (UTF-8, separado por ``;`` o ``,``, con encabezado): ``nombre`` (obligatorio); ``nit``,
``odoo_partner_id``, ``latitud``, ``longitud``, ``ciudad`` y ``direccion`` (opcionales). Latitud y
longitud van juntas, en grados decimales (rango -90..90 y -180..180); con ``;`` como separador se acepta
también la coma decimal (``6,167371``). Se actualiza por NIT o, sin NIT, por nombre normalizado::

    python -m app.combustible.proveedores cargar proveedores.csv [--resolver-odoo] [--reemplazar]
    python -m app.combustible.proveedores listar
    python -m app.combustible.proveedores desactivar 3 7

``--resolver-odoo`` busca en Odoo (solo lectura) el contacto de las filas sin
``odoo_partner_id``: por NIT y, si no, por nombre exacto; solo si el resultado es único.
``--reemplazar`` desactiva los proveedores que no vengan en el archivo.

El Panel Admin (``app/admin``) usa ``analizar_csv``, ``previsualizar`` y ``cargar`` para la carga con vista previa.
"""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import csv_util
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
    latitud: float | None = None
    longitud: float | None = None
    ciudad: str | None = None
    direccion: str | None = None


@dataclass(frozen=True)
class ErrorFila:
    linea: int
    mensaje: str


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


def analizar_csv(texto: str) -> tuple[list[tuple[int, FilaProveedor]], list[ErrorFila]]:
    """Filas válidas (con su número de línea) y errores por fila.

    Lanza ValueError si falta la columna ``nombre``.
    """
    filas: list[tuple[int, FilaProveedor]] = []
    errores: list[ErrorFila] = []
    for linea, fila in csv_util.leer_filas(
            texto, "El CSV debe tener la columna 'nombre' (opcionales: 'nit', 'odoo_partner_id', "
                   "'latitud', 'longitud', 'ciudad', 'direccion')"):
        if fila is None:
            errores.append(ErrorFila(linea, csv_util.MSG_MAS_COLUMNAS))
            continue
        nombre = " ".join(fila.get("nombre", "").split())
        if not nombre:
            continue
        try:
            partner = fila.get("odoo_partner_id") or ""
            if partner and not partner.isdigit():
                raise ValueError(f"odoo_partner_id debe ser un número ({partner!r})")
            lat = csv_util.decimal(fila.get("latitud", ""), "latitud", 90)
            lon = csv_util.decimal(fila.get("longitud", ""), "longitud", 180)
            if (lat is None) != (lon is None):
                raise ValueError("latitud y longitud deben venir juntas")
        except ValueError as exc:
            errores.append(ErrorFila(linea, str(exc)))
            continue
        filas.append((linea, FilaProveedor(
            nombre[:255], normalizar_nit(fila.get("nit")), int(partner) if partner else None, lat, lon,
            csv_util.texto_limpio(fila.get("ciudad", ""), 120), csv_util.texto_limpio(fila.get("direccion", ""), 255))))
    return filas, errores


def leer_csv(texto: str) -> list[FilaProveedor]:
    """Filas del CSV. Lanza ValueError con la línea si un dato es inválido."""
    filas, errores = analizar_csv(texto)
    if errores:
        raise ValueError(f"Línea {errores[0].linea}: {errores[0].mensaje}")
    return [fila for _, fila in filas]


def listar_activos(session: Session) -> list[ProveedorCombustible]:
    return list(session.scalars(
        select(ProveedorCombustible).where(ProveedorCombustible.activo.is_(True)).order_by(ProveedorCombustible.nombre)
    ))


def resolver_contactos(filas: Iterable[FilaProveedor], resolver: ResolverContacto) -> list[FilaProveedor]:
    """Completa ``odoo_partner_id`` de las filas que no lo traen (fuera de cualquier transacción)."""
    out = []
    for fila in filas:
        if fila.odoo_partner_id is None:
            fila = replace(fila, odoo_partner_id=resolver(fila.nit, fila.nombre))
        out.append(fila)
    return out


def _buscar(session: Session, fila: FilaProveedor, clave: str) -> ProveedorCombustible | None:
    condiciones = [ProveedorCombustible.clave == clave]
    if fila.nit:
        condiciones.append(ProveedorCombustible.nit == fila.nit)
    existentes = list(session.scalars(select(ProveedorCombustible).where(or_(*condiciones))))
    # El NIT manda: si hay uno con ese NIT, es ese aunque el nombre haya cambiado.
    return next((p for p in existentes if fila.nit and p.nit == fila.nit), existentes[0] if existentes else None)


def previsualizar(db: Database, filas: list[FilaProveedor], reemplazar: bool = False) -> dict[str, int]:
    """Lo que haría ``cargar`` (nuevos, actualizados, desactivados) sin escribir nada."""
    nuevos = actualizados = 0
    vistos: set[int] = set()
    with db.session() as session:
        for fila in filas:
            clave = normalizar_nombre(fila.nombre)
            if not clave:
                continue
            prov = _buscar(session, fila, clave)
            if prov is None:
                nuevos += 1
            else:
                actualizados += 1
                vistos.add(prov.id)
        desactivados = sum(1 for p in listar_activos(session) if p.id not in vistos) if reemplazar else 0
    return {"nuevos": nuevos, "actualizados": actualizados, "desactivados": desactivados}


def cargar(db: Database, filas: list[FilaProveedor], reemplazar: bool = False) -> ResumenCarga:
    """UPSERT por NIT o por nombre normalizado. Reactiva los que vuelven a venir en el archivo."""
    nuevos = actualizados = desactivados = 0
    vistos: set[int] = set()
    with db.session() as session:
        for fila in filas:
            clave = normalizar_nombre(fila.nombre)
            if not clave:
                continue
            prov = _buscar(session, fila, clave)
            if prov is None:
                prov = ProveedorCombustible(nombre=fila.nombre, clave=clave, nit=fila.nit,
                                            odoo_partner_id=fila.odoo_partner_id, activo=True,
                                            latitud=fila.latitud, longitud=fila.longitud,
                                            ciudad=fila.ciudad, direccion=fila.direccion)
                session.add(prov)
                nuevos += 1
            else:
                prov.nombre, prov.clave, prov.activo = fila.nombre, clave, True
                prov.nit = fila.nit or prov.nit
                prov.odoo_partner_id = fila.odoo_partner_id or prov.odoo_partner_id
                if fila.latitud is not None and fila.longitud is not None:
                    prov.latitud, prov.longitud = fila.latitud, fila.longitud
                prov.ciudad = fila.ciudad or prov.ciudad
                prov.direccion = fila.direccion or prov.direccion
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


def set_activo(db: Database, proveedor_id: int, activo: bool) -> str | None:
    """Activa o desactiva una estación. Devuelve su nombre, o None si no existe."""
    with db.session() as session:
        prov = session.get(ProveedorCombustible, proveedor_id)
        if prov is None:
            return None
        prov.activo = activo
        return prov.nombre


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
                print(f"{p.id:>4}  {p.nombre}  NIT={p.nit or '—'}  contacto_odoo={p.odoo_partner_id or '—'}  "
                      f"ubicación={'sí' if p.latitud is not None else '—'}  ciudad={p.ciudad or '—'}")
        return
    if args.cmd == "desactivar":
        print(f"Desactivados: {desactivar(db, args.ids)}")
        return
    filas = leer_csv(csv_util.decodificar(args.archivo.read_bytes()))
    if args.resolver_odoo:
        filas = resolver_contactos(filas, build_writer(settings).buscar_contacto)
    resumen = cargar(db, filas, reemplazar=args.reemplazar)
    print(f"Nuevos: {resumen.nuevos}  Actualizados: {resumen.actualizados}  Desactivados: {resumen.desactivados}")
    if resumen.sin_contacto:
        print("Sin contacto de Odoo (irán como texto en 'supplier'):", "; ".join(resumen.sin_contacto))


if __name__ == "__main__":  # pragma: no cover
    main()
