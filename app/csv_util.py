"""Utilidades comunes de los CSV que cargan los administradores (estaciones de combustible, locales autorizados).

UTF-8 (con o sin BOM) o Windows-1252 (CSV de Excel en español), separador ``;`` o ``,`` y coma decimal aceptada
en coordenadas. Las usan ``app.combustible.proveedores`` y ``app.gastos_operativos.locales``.
"""
from __future__ import annotations

import csv
import io
import re


def decodificar(datos: bytes) -> str:
    """Texto de un CSV: UTF-8 (con o sin BOM) y, si no lo es, Windows-1252."""
    try:
        return datos.decode("utf-8-sig")
    except UnicodeDecodeError:
        return datos.decode("cp1252", errors="replace")


def decimal(texto: str, campo: str, limite: float) -> float | None:
    """Grados decimales con punto o coma (el separador del CSV puede ser ``;``). Vacío -> None."""
    t = texto.strip().replace(",", ".")
    if not t:
        return None
    if not re.fullmatch(r"-?\d+(\.\d+)?", t):
        raise ValueError(f"{campo} no es un número válido ({texto!r})")
    valor = float(t)
    if not -limite <= valor <= limite:
        raise ValueError(f"{campo} fuera de rango (-{limite:g} a {limite:g}): {texto!r}")
    return valor


def texto_limpio(valor: str, maximo: int) -> str | None:
    """Espacios colapsados y recortado a ``maximo`` caracteres; vacío -> None."""
    return " ".join(valor.split())[:maximo] or None


def leer_filas(texto: str, ayuda_columnas: str) -> list[tuple[int, dict[str, str] | None]]:
    """Filas del CSV como ``(línea, dict)`` con encabezados en minúsculas y valores sin espacios sobrantes.

    ``None`` en lugar del dict = la fila tiene más columnas que encabezados (típico de una coma decimal sin
    comillas en un CSV separado por comas). Lanza ValueError con ``ayuda_columnas`` si falta ``nombre``.
    """
    texto = texto.lstrip("﻿")
    muestra = texto[:2048]
    delimitador = ";" if muestra.count(";") >= muestra.count(",") else ","
    lector = csv.DictReader(io.StringIO(texto), delimiter=delimitador)
    columnas = {(c or "").strip().lower() for c in (lector.fieldnames or [])}
    if "nombre" not in columnas:
        raise ValueError(ayuda_columnas)
    filas: list[tuple[int, dict[str, str] | None]] = []
    for linea, raw in enumerate(lector, start=2):
        if None in raw:
            filas.append((linea, None))
        else:
            filas.append((linea, {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}))
    return filas


MSG_MAS_COLUMNAS = "Hay más columnas que encabezados (¿coma decimal sin comillas?)"
