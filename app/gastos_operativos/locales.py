"""Restaurantes, hoteles y otros locales autorizados para Gastos Operativos: lista local que carga el negocio por CSV.

La ubicación de cada local sirve para cotejar la del gasto (``app.gastos_operativos.cotejo``): el cotejo solo se
almacena, no va a Odoo ni bloquea el envío.

CSV (UTF-8 o Windows-1252, separado por ``;`` o ``,``, con encabezado)::

    nombre;nit;categoria;latitud;longitud;ciudad;direccion

``nombre``, ``latitud`` y ``longitud`` son obligatorios (grados decimales, rango -90..90 y -180..180; con ``;`` como
separador se acepta la coma decimal). ``categoria`` es el nombre de un tipo de ``GASTOS_OPERATIVOS_TIPOS`` (sin
distinguir tildes ni mayúsculas); vacía = el local vale para todos los tipos. Se actualiza por NIT o, sin NIT, por
nombre normalizado más categoría::

    python -m app.gastos_operativos.locales cargar locales.csv [--reemplazar]
    python -m app.gastos_operativos.locales listar
    python -m app.gastos_operativos.locales desactivar 3 7

``--reemplazar`` desactiva los locales que no vengan en el archivo (y exige que no tenga errores).
El Panel Admin (``app/admin``) usa ``analizar_csv``, ``previsualizar`` y ``cargar`` para la carga con vista previa.
"""
from __future__ import annotations

import argparse
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import csv_util
from ..combustible.proveedores import ErrorFila, normalizar_nit
from ..preop.db import Database
from ..preop.models import LocalAutorizado
from ..prueba_ruta.acuerdos import normalizar_acuerdo as normalizar_nombre
from .catalogo import TipoGasto, clave_tipo, parse_tipos

NOMBRE_MAX = 255


@dataclass(frozen=True)
class FilaLocal:
    nombre: str
    latitud: float
    longitud: float
    nit: str | None = None
    categoria: str | None = None  # nombre canónico del tipo (None = todos)
    ciudad: str | None = None
    direccion: str | None = None


@dataclass(frozen=True)
class ResumenCarga:
    nuevos: int
    actualizados: int
    desactivados: int


def categorias_de(tipos: Iterable[TipoGasto]) -> dict[str, str]:
    """``{clave sin tildes: nombre del tipo}`` para validar y canonizar la columna ``categoria``."""
    return {clave_tipo(t.nombre): t.nombre for t in tipos}


def categorias_por_defecto() -> dict[str, str]:
    from ..config import Settings

    return categorias_de(parse_tipos(Settings.model_fields["gastos_operativos_tipos"].default))


def analizar_csv(texto: str, categorias: dict[str, str]) -> tuple[list[tuple[int, FilaLocal]], list[ErrorFila]]:
    """Filas válidas (con su número de línea) y errores por fila. ``categorias`` viene de ``categorias_de``.

    Lanza ValueError si falta la columna ``nombre``.
    """
    filas: list[tuple[int, FilaLocal]] = []
    errores: list[ErrorFila] = []
    for linea, fila in csv_util.leer_filas(
            texto, "El CSV debe tener la columna 'nombre' (más 'latitud' y 'longitud' obligatorias; opcionales: "
                   "'nit', 'categoria', 'ciudad', 'direccion')"):
        if fila is None:
            errores.append(ErrorFila(linea, csv_util.MSG_MAS_COLUMNAS))
            continue
        nombre = " ".join(fila.get("nombre", "").split())
        if not nombre:
            continue
        try:
            if len(nombre) > NOMBRE_MAX:
                raise ValueError(f"el nombre supera {NOMBRE_MAX} caracteres")
            lat = csv_util.decimal(fila.get("latitud", ""), "latitud", 90)
            lon = csv_util.decimal(fila.get("longitud", ""), "longitud", 180)
            if lat is None or lon is None:
                raise ValueError("latitud y longitud son obligatorias")
            categoria = None
            bruta = " ".join(fila.get("categoria", "").split())
            if bruta:
                categoria = categorias.get(clave_tipo(bruta))
                if categoria is None:
                    raise ValueError(f"la categoría «{bruta}» no es un tipo de gasto configurado "
                                     f"({', '.join(categorias.values())})")
        except ValueError as exc:
            errores.append(ErrorFila(linea, str(exc)))
            continue
        filas.append((linea, FilaLocal(
            nombre, lat, lon, normalizar_nit(fila.get("nit")), categoria,
            csv_util.texto_limpio(fila.get("ciudad", ""), 120), csv_util.texto_limpio(fila.get("direccion", ""), 255))))
    return filas, errores


def leer_csv(texto: str, categorias: dict[str, str]) -> list[FilaLocal]:
    """Filas del CSV. Lanza ValueError con la línea si un dato es inválido."""
    filas, errores = analizar_csv(texto, categorias)
    if errores:
        raise ValueError(f"Línea {errores[0].linea}: {errores[0].mensaje}")
    return [fila for _, fila in filas]


def listar_activos(session: Session) -> list[LocalAutorizado]:
    return list(session.scalars(
        select(LocalAutorizado).where(LocalAutorizado.activo.is_(True)).order_by(LocalAutorizado.nombre)))


def _buscar(session: Session, fila: FilaLocal, clave: str) -> LocalAutorizado | None:
    """Por NIT; si no, por nombre normalizado + categoría (un registro sin NIT puede adoptar el NIT que llega)."""
    if fila.nit:
        encontrado = session.scalar(select(LocalAutorizado).where(LocalAutorizado.nit == fila.nit))
        if encontrado is not None:
            return encontrado
    candidatos = session.scalars(select(LocalAutorizado).where(LocalAutorizado.clave == clave))
    for local in candidatos:
        if local.categoria == fila.categoria and (not fila.nit or local.nit is None):
            return local
    return None


def previsualizar(db: Database, filas: list[FilaLocal], reemplazar: bool = False) -> dict[str, int]:
    """Lo que haría ``cargar`` (nuevos, actualizados, desactivados) sin escribir nada."""
    nuevos = actualizados = 0
    vistos: set[int] = set()
    nuevos_nit: set[str] = set()
    nuevos_nombre: dict[tuple[str, str | None], str | None] = {}
    with db.session() as session:
        for fila in filas:
            clave = normalizar_nombre(fila.nombre)
            if not clave:
                continue
            local = _buscar(session, fila, clave)
            if local is not None:
                actualizados += 1
                vistos.add(local.id)
                continue
            llave = (clave, fila.categoria)  # repetida dentro del archivo: la 2.ª actualiza a la 1.ª
            if (fila.nit and fila.nit in nuevos_nit) or (
                    llave in nuevos_nombre and (not fila.nit or nuevos_nombre[llave] is None)):
                actualizados += 1
            else:
                nuevos += 1
                nuevos_nombre[llave] = fila.nit
                if fila.nit:
                    nuevos_nit.add(fila.nit)
        desactivados = sum(1 for p in listar_activos(session) if p.id not in vistos) if reemplazar else 0
    return {"nuevos": nuevos, "actualizados": actualizados, "desactivados": desactivados}


def cargar(db: Database, filas: list[FilaLocal], reemplazar: bool = False) -> ResumenCarga:
    """UPSERT por NIT o por nombre normalizado + categoría. Reactiva los que vuelven a venir en el archivo."""
    nuevos = actualizados = desactivados = 0
    vistos: set[int] = set()
    with db.session() as session:
        for fila in filas:
            clave = normalizar_nombre(fila.nombre)
            if not clave:
                continue
            local = _buscar(session, fila, clave)
            if local is None:
                local = LocalAutorizado(nombre=fila.nombre, clave=clave, nit=fila.nit, categoria=fila.categoria,
                                        latitud=fila.latitud, longitud=fila.longitud, ciudad=fila.ciudad,
                                        direccion=fila.direccion, activo=True)
                session.add(local)
                nuevos += 1
            else:
                local.nombre, local.clave, local.activo = fila.nombre, clave, True
                local.nit = fila.nit or local.nit
                local.categoria = fila.categoria
                local.latitud, local.longitud = fila.latitud, fila.longitud
                local.ciudad = fila.ciudad or local.ciudad
                local.direccion = fila.direccion or local.direccion
                actualizados += 1
            session.flush()
            vistos.add(local.id)
        if reemplazar:
            for local in listar_activos(session):
                if local.id not in vistos:
                    local.activo = False
                    desactivados += 1
    return ResumenCarga(nuevos, actualizados, desactivados)


def desactivar(db: Database, ids: list[int]) -> int:
    with db.session() as session:
        locales = list(session.scalars(select(LocalAutorizado).where(LocalAutorizado.id.in_(ids))))
        for local in locales:
            local.activo = False
        return len(locales)


def set_activo(db: Database, local_id: int, activo: bool) -> str | None:
    """Activa o desactiva un local. Devuelve su nombre, o None si no existe."""
    with db.session() as session:
        local = session.get(LocalAutorizado, local_id)
        if local is None:
            return None
        local.activo = activo
        return local.nombre


def main() -> None:  # pragma: no cover - CLI de operación
    from ..config import get_settings

    parser = argparse.ArgumentParser(description="Locales autorizados para Gastos Operativos")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_cargar = sub.add_parser("cargar", help="carga o actualiza locales desde un CSV")
    p_cargar.add_argument("archivo", type=Path)
    p_cargar.add_argument("--reemplazar", action="store_true", help="desactiva los que no estén en el archivo")
    sub.add_parser("listar", help="lista los locales activos")
    p_des = sub.add_parser("desactivar", help="desactiva locales por id")
    p_des.add_argument("ids", type=int, nargs="+")
    args = parser.parse_args()

    settings = get_settings()
    db = Database(settings.database_url)
    if args.cmd == "listar":
        with db.session() as session:
            for p in listar_activos(session):
                print(f"{p.id:>4}  {p.nombre}  NIT={p.nit or '—'}  categoría={p.categoria or '(todas)'}  "
                      f"ubicación={p.latitud:.6f},{p.longitud:.6f}  ciudad={p.ciudad or '—'}")
        return
    if args.cmd == "desactivar":
        print(f"Desactivados: {desactivar(db, args.ids)}")
        return
    categorias = categorias_de(parse_tipos(settings.gastos_operativos_tipos))
    filas = leer_csv(csv_util.decodificar(args.archivo.read_bytes()), categorias)
    resumen = cargar(db, filas, reemplazar=args.reemplazar)
    print(f"Nuevos: {resumen.nuevos}  Actualizados: {resumen.actualizados}  Desactivados: {resumen.desactivados}")


if __name__ == "__main__":  # pragma: no cover
    main()
