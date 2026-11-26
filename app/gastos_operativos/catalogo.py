"""Catálogo del formulario de Gastos Operativos: tipos configurables (``GASTOS_OPERATIVOS_TIPOS``) y límites.

``GASTOS_OPERATIVOS_TIPOS`` = ``Nombre:product_id`` separados por ``;``, p. ej.
``Alimentación:1;Hospedaje:2;Peajes:6;Parqueadero:6;Lavado:6;Otros:6``. El ``product_id`` es el de
``product.product`` en el ``hr.expense`` y cambia por ambiente (qa2: Meals=1, Travel & Accommodation=2, Others=6).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from ..combustible.catalogo import DIAS_ATRAS_MAX

NOMBRE_TIPO_MAX = 60
MAX_GASTOS = 10  # tarjetas por envío
VALOR_MAX = 100_000_000  # COP por gasto: tope de sanidad contra errores de digitación
OBSERVACION_MAX = 500
# Soportes (PDF o foto) por gasto y en total: el proxy (nginx) acepta 10 MB por petición y el base64 pesa 4/3.
SOPORTE_MAX_BYTES = 3 * 1024 * 1024
SOPORTES_TOTAL_MAX_BYTES = 6 * 1024 * 1024
FORMATO_TIPOS = "Nombre:product_id separados por «;» (p. ej. Alimentación:1;Hospedaje:2;Otros:6)"
FORMATO_TOPES = "Nombre:valor en COP separados por «;» (p. ej. Alimentación:50000;Hospedaje:150000)"


class TiposGastoError(ValueError):
    """``GASTOS_OPERATIVOS_TIPOS`` mal formado."""


class TopesGastoError(ValueError):
    """``GASTOS_OPERATIVOS_TOPES`` mal formado o con un tipo que no existe."""


def clave_tipo(nombre: str | None) -> str:
    """Nombre de un tipo sin tildes, en minúsculas y con espacios colapsados (``Alimentación`` == ``alimentacion``)."""
    texto = unicodedata.normalize("NFKD", nombre or "")
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
    return " ".join(texto.casefold().split())


@dataclass(frozen=True)
class TipoGasto:
    nombre: str
    product_id: int


def parse_tipos(texto: str | None) -> tuple[TipoGasto, ...]:
    """Convierte el texto del ``.env`` en tipos. Lanza ``TiposGastoError`` con un mensaje claro."""
    partes = [p.strip() for p in (texto or "").split(";") if p.strip()]
    if not partes:
        raise TiposGastoError(f"GASTOS_OPERATIVOS_TIPOS está vacío: usa el formato {FORMATO_TIPOS}")
    tipos: list[TipoGasto] = []
    vistos: set[str] = set()
    for parte in partes:
        nombre, sep, ident = parte.rpartition(":")
        nombre, ident = " ".join(nombre.split()), ident.strip()
        if not sep or not nombre:
            raise TiposGastoError(f"GASTOS_OPERATIVOS_TIPOS: «{parte}» no tiene el formato Nombre:product_id "
                                  f"(usa {FORMATO_TIPOS})")
        if not re.fullmatch(r"\d+", ident) or int(ident) <= 0:
            raise TiposGastoError(f"GASTOS_OPERATIVOS_TIPOS: el product_id de «{nombre}» debe ser un número "
                                  f"entero mayor que cero (llegó «{ident}»)")
        if len(nombre) > NOMBRE_TIPO_MAX:
            raise TiposGastoError(f"GASTOS_OPERATIVOS_TIPOS: el nombre «{nombre[:20]}…» supera "
                                  f"{NOMBRE_TIPO_MAX} caracteres")
        clave = nombre.casefold()
        if clave in vistos:
            raise TiposGastoError(f"GASTOS_OPERATIVOS_TIPOS: el tipo «{nombre}» está repetido")
        vistos.add(clave)
        tipos.append(TipoGasto(nombre, int(ident)))
    return tuple(tipos)


def texto_tipos(tipos: tuple[TipoGasto, ...]) -> str:
    return ";".join(f"{t.nombre}:{t.product_id}" for t in tipos)


def parse_topes(texto: str | None, tipos: tuple[TipoGasto, ...] | None = None) -> dict[str, int]:
    """Convierte ``GASTOS_OPERATIVOS_TOPES`` en ``{nombre del tipo: tope en COP}``.

    Vacío = sin topes. Con ``tipos`` cada nombre debe existir (sin distinguir tildes ni mayúsculas) y la clave
    del resultado es el nombre tal como está en ``GASTOS_OPERATIVOS_TIPOS``; sin ``tipos`` solo se valida el
    formato. Un tipo ausente no tiene tope. Lanza ``TopesGastoError`` con un mensaje claro.
    """
    por_clave = {clave_tipo(t.nombre): t.nombre for t in tipos} if tipos is not None else None
    topes: dict[str, int] = {}
    vistos: set[str] = set()
    for parte in (p.strip() for p in (texto or "").split(";") if p.strip()):
        nombre, sep, valor = parte.rpartition(":")
        nombre, valor = " ".join(nombre.split()), valor.strip()
        if not sep or not nombre:
            raise TopesGastoError(f"GASTOS_OPERATIVOS_TOPES: «{parte}» no tiene el formato Nombre:valor "
                                  f"(usa {FORMATO_TOPES})")
        if not re.fullmatch(r"\d+", valor) or int(valor) <= 0:
            raise TopesGastoError(f"GASTOS_OPERATIVOS_TOPES: el tope de «{nombre}» debe ser un número entero en "
                                  f"COP mayor que cero, sin puntos ni símbolos (llegó «{valor}»)")
        clave = clave_tipo(nombre)
        if clave in vistos:
            raise TopesGastoError(f"GASTOS_OPERATIVOS_TOPES: el tipo «{nombre}» está repetido")
        vistos.add(clave)
        if por_clave is not None:
            if clave not in por_clave:
                raise TopesGastoError(f"GASTOS_OPERATIVOS_TOPES: el tipo «{nombre}» no existe en "
                                      f"GASTOS_OPERATIVOS_TIPOS ({', '.join(por_clave.values())})")
            nombre = por_clave[clave]
        topes[nombre] = int(valor)
    return topes


def texto_topes(topes: dict[str, int]) -> str:
    return ";".join(f"{nombre}:{valor}" for nombre, valor in topes.items())


def form_definition(tipos: tuple[TipoGasto, ...], topes: dict[str, int] | None = None) -> dict[str, Any]:
    """Catálogo para la Mini App. El ``product_id`` no sale del servidor: el cliente elige por nombre.

    ``tope`` (COP por gasto) es ``None`` si el tipo no tiene máximo.
    """
    topes = topes or {}
    return {
        "tipos": [{"value": t.nombre, "label": t.nombre, "tope": topes.get(t.nombre)} for t in tipos],
        "dias_atras_max": DIAS_ATRAS_MAX,
        "max_gastos": MAX_GASTOS,
        "valor_max": VALOR_MAX,
        "observacion_max": OBSERVACION_MAX,
        "soporte_max_bytes": SOPORTE_MAX_BYTES,
        "soportes_total_max_bytes": SOPORTES_TOTAL_MAX_BYTES,
    }
