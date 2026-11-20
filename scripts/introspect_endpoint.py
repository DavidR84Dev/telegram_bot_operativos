#!/usr/bin/env python3
"""Introspección de un endpoint de la API REST de Odoo -> modelos SQLAlchemy + Pydantic.

Descarga una muestra de ``items`` (o lee un JSON guardado), infiere el tipo de
cada campo recorriendo TODAS las filas de la muestra (un campo vacío en una fila
no fija el tipo) y genera código listo para revisar y pegar.

Ejemplos::

    # Contra la API (credenciales en .env: ODOO_API_URL/ODOO_URL, ODOO_API_LOGIN, ODOO_API_PASSWORD)
    python scripts/introspect_endpoint.py /api/preoperacional --company 1 --sample 200 --class Preop

    # Desde un archivo (respuesta guardada con curl)
    python scripts/introspect_endpoint.py --file respuesta.json --class Preop

Reglas de inferencia:
    bool -> Boolean | int -> Integer (BigInteger si > 2^31) | float/int mezclados -> Float
    str ISO fecha-hora -> DateTime(timezone=True) | str ISO fecha -> Date
    str -> String(n) con n = potencia de 2 >= 2x la longitud máxima vista (o Text si > 1024)
    dict/list -> JSONB | campo siempre nulo/vacío -> String(255) (revisar)
"""
from __future__ import annotations

import argparse
import json
import keyword
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:?\d{2}|Z)?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _kind(value: Any) -> str | None:
    if value is None or (isinstance(value, str) and value == ""):
        return None
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, (dict, list)):
        return "json"
    if isinstance(value, str):
        if _DATETIME_RE.match(value):
            return "datetime"
        if _DATE_RE.match(value):
            return "date"
        return "str"
    return "json"


def infer(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    kinds: dict[str, set[str]] = defaultdict(set)
    max_len: dict[str, int] = defaultdict(int)
    max_int: dict[str, int] = defaultdict(int)
    nullable: dict[str, bool] = defaultdict(bool)
    order: list[str] = []
    for row in items:
        for key, value in row.items():
            if key not in kinds:
                order.append(key)
                kinds[key]  # registra la clave aunque todos sus valores sean vacíos
            kind = _kind(value)
            if kind is None:
                nullable[key] = True
                continue
            kinds[key].add(kind)
            if kind == "str":
                max_len[key] = max(max_len[key], len(value))
            if kind == "int":
                max_int[key] = max(max_int[key], abs(value))
        for key in order:
            if key not in row:
                nullable[key] = True

    result: dict[str, dict[str, Any]] = {}
    for key in order:
        found = kinds[key]
        if found == {"int", "float"} or found == {"float"}:
            kind = "float"
        elif len(found) == 1:
            kind = next(iter(found))
        elif not found:
            kind = "unknown"
        else:
            kind = "str" if "str" in found else "json"  # mezcla incompatible: se conserva como texto/JSON
        result[key] = {"kind": kind, "nullable": nullable[key] or not found, "max_len": max_len[key],
                       "big": max_int[key] >= 2**31}
    return result


def _identifier(name: str) -> str:
    ident = re.sub(r"\W", "_", name).lower()
    if ident[:1].isdigit() or keyword.iskeyword(ident):
        ident = f"f_{ident}"
    return ident


def _string_len(n: int) -> int:
    size = 16
    while size < n * 2:
        size *= 2
    return size


def _sa_type(spec: dict[str, Any]) -> tuple[str, str]:
    kind = spec["kind"]
    if kind == "bool":
        return "Boolean", "bool"
    if kind == "int":
        return ("BigInteger" if spec["big"] else "Integer"), "int"
    if kind == "float":
        return "Float", "float"
    if kind == "datetime":
        return "DateTime(timezone=True)", "datetime"
    if kind == "date":
        return "Date", "date"
    if kind == "json":
        return "JsonType", "Any"
    if kind == "str" and spec["max_len"] > 1024:
        return "Text", "str"
    if kind == "str":
        return f"String({_string_len(spec['max_len'])})", "str"
    return "String(255)", "str"  # unknown: siempre vacío en la muestra


def render(fields: dict[str, dict[str, Any]], class_name: str, table: str) -> str:
    lines = [
        "# ─── Generado por scripts/introspect_endpoint.py: REVISAR antes de usar ───",
        "from datetime import date, datetime",
        "from typing import Any",
        "",
        "from pydantic import BaseModel, ConfigDict, Field",
        "from sqlalchemy import JSON, BigInteger, Boolean, Date, DateTime, Float, Integer, String, Text",
        "from sqlalchemy.dialects.postgresql import JSONB",
        "from sqlalchemy.orm import Mapped, mapped_column",
        "",
        "from app.preop.models import Base, TimestampMixin",
        "",
        'JsonType = JSON().with_variant(JSONB(), "postgresql")',
        "",
        "",
        f"class {class_name}(TimestampMixin, Base):",
        f'    __tablename__ = "{table}"',
        "",
        "    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)",
        "    odoo_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)",
        '    sync_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pendiente")',
        "    error_log: Mapped[str | None] = mapped_column(Text)",
        '    extra: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)',
    ]
    for name, spec in fields.items():
        if name == "id":
            continue
        sa_type, py_type = _sa_type(spec)
        attr = _identifier(name)
        optional = " | None" if spec["nullable"] else ""
        key = "" if attr == name else f'"{name}", '
        note = "  # siempre vacío en la muestra: revisar tipo" if spec["kind"] == "unknown" else ""
        lines.append(f"    {attr}: Mapped[{py_type}{optional}] = mapped_column({key}{sa_type}, "
                     f"nullable={spec['nullable']}){note}")

    lines += ["", "", f"class {class_name}Api(BaseModel):",
              '    model_config = ConfigDict(extra="ignore", populate_by_name=True)', ""]
    for name, spec in fields.items():
        _sa, py_type = _sa_type(spec)
        attr = _identifier(name)
        alias = "" if attr == name else f'alias="{name}"'
        if spec["nullable"]:
            default = f"Field(default=None{', ' + alias if alias else ''})"
            lines.append(f"    {attr}: {py_type} | None = {default}")
        else:
            lines.append(f"    {attr}: {py_type}" + (f" = Field({alias})" if alias else ""))
    return "\n".join(lines) + "\n"


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def fetch_sample(endpoint: str, company: int, sample: int) -> list[dict[str, Any]]:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.preop.rest_client import OdooRestClient

    _load_env(Path(__file__).resolve().parent.parent / ".env")
    base = os.environ.get("ODOO_API_URL") or os.environ["ODOO_URL"]
    with OdooRestClient(base, os.environ["ODOO_API_LOGIN"], os.environ["ODOO_API_PASSWORD"]) as client:
        items: list[dict[str, Any]] = []
        for item in client.iter_items(endpoint, company, page_size=min(sample, 500)):
            items.append(item)
            if len(items) >= sample:
                break
    return items


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("endpoint", nargs="?", help="p. ej. /api/preoperacional")
    parser.add_argument("--file", type=Path, help="JSON guardado ({'items': [...]} o lista)")
    parser.add_argument("--company", type=int, default=1)
    parser.add_argument("--sample", type=int, default=200)
    parser.add_argument("--class", dest="class_name", default="Preoperacional")
    parser.add_argument("--table", default=None)
    parser.add_argument("--json-schema", action="store_true", help="imprime también el resumen de tipos inferidos")
    args = parser.parse_args()

    if args.file:
        data = json.loads(args.file.read_text(encoding="utf-8"))
        items = data["items"] if isinstance(data, dict) else data
    elif args.endpoint:
        items = fetch_sample(args.endpoint, args.company, args.sample)
    else:
        parser.error("indica un endpoint o --file")
    if not items:
        sys.exit("La muestra está vacía: no hay nada que inferir")

    fields = infer(items)
    table = args.table or re.sub(r"(?<!^)(?=[A-Z])", "_", args.class_name).lower()
    if args.json_schema:
        print(json.dumps(fields, indent=2, default=str), file=sys.stderr)
    print(f"# Muestra: {len(items)} registros, {len(fields)} campos ({datetime.now():%Y-%m-%d %H:%M})")
    print(render(fields, args.class_name, table))


if __name__ == "__main__":
    main()
