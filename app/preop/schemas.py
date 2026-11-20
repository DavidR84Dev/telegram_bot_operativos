"""Esquemas Pydantic: entrada de la Mini App y registros de la API REST de Odoo.

Entrada (``PreoperacionalIn``): estricta. Rechaza campos desconocidos, valida
cada ítem contra el catálogo (``checklist.py``) y acota longitudes y rangos.

Registros de la API (``*Api``): tolerantes. La API puede devolver un campo como
texto plano (``"responsable": "Ana"``) o como objeto (``{"name": "Ana",
"mobile": "300..."}``); ambos se normalizan. Los campos extra se ignoran (pero
el objeto completo se guarda en ``raw``).
"""
from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .checklist import CHECKLIST, CHECKLIST_BY_KEY, MEASURES_BY_KEY

# ─────────────────────────────── Normalización de la API ───────────────────────────────


def _clean_str(value: Any) -> str | None:
    """'' / False / None -> None (Odoo usa False y la API usa '' para vacío)."""
    if value is None or value is False:
        return None
    text = str(value).strip()
    return text or None


def related_name(value: Any) -> str | None:
    """Nombre de un relacional: 'Texto', {'name'|'nombre': ...} o [id, 'Texto'] (formato Odoo)."""
    if isinstance(value, dict):
        return _clean_str(value.get("name") or value.get("nombre") or value.get("display_name"))
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return _clean_str(value[1])
    return _clean_str(value)


def related_id(value: Any) -> int | None:
    """Id de un relacional: 7, {'id': 7}, [7, 'Texto']. Un texto no numérico devuelve None."""
    if isinstance(value, dict):
        value = value.get("id")
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        value = value[0]
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def related_attr(value: Any, attr: str) -> str | None:
    """Atributo anidado (p. ej. ``responsable.mobile``) si la API entrega un objeto."""
    if isinstance(value, dict):
        return _clean_str(value.get(attr))
    return None


def parse_odoo_datetime(value: Any) -> datetime | None:
    """Odoo serializa en UTC sin zona ('2026-11-20 13:00:00' o ISO); se devuelve aware UTC."""
    if value in (None, "", False):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class _ApiRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int = Field(gt=0)
    company_id: Any = None


class EmpleadoApi(_ApiRecord):
    nombre: str = Field(min_length=1)
    departamento: Any = None
    puesto_trabajo: Any = None
    codigo_pin: Any = None
    id_contract: Any = None
    contrato: Any = None
    contrato_temporal_id: Any = None
    contrato_temporal: Any = None
    responsable: Any = None

    def to_row(self) -> dict[str, Any]:
        return {
            "nombre": self.nombre.strip(),
            "departamento_id": related_id(self.departamento),
            "departamento": related_name(self.departamento) if not isinstance(self.departamento, int) else None,
            "puesto_trabajo": related_name(self.puesto_trabajo),
            "codigo_pin": _clean_str(self.codigo_pin),
            "id_contract": related_id(self.id_contract),
            "contrato": related_name(self.contrato),
            "contrato_temporal_id": related_id(self.contrato_temporal_id),
            "contrato_temporal": related_name(self.contrato_temporal),
            "responsable": related_name(self.responsable),
            "responsable_mobile": related_attr(self.responsable, "mobile"),
            "company_id": related_name(self.company_id),
        }


class FlotaApi(_ApiRecord):
    nombre: str = Field(min_length=1)
    matricula: Any = None
    estado: Any = None
    numero_interno: Any = None
    parque_automotor: Any = None
    odometer_post: Any = None
    tipo_solicitud: Any = None

    def to_row(self) -> dict[str, Any]:
        try:
            odometer = float(self.odometer_post) if self.odometer_post not in (None, "", False) else None
        except (TypeError, ValueError):
            odometer = None
        return {
            "nombre": self.nombre.strip(),
            "matricula": _clean_str(self.matricula),
            "estado": related_name(self.estado),
            "numero_interno": _clean_str(self.numero_interno),
            "parque_automotor": _clean_str(self.parque_automotor),
            "odometer_post": odometer,
            "tipo_solicitud": _clean_str(self.tipo_solicitud),
            "company_id": related_name(self.company_id),
        }


class ExtractoApi(_ApiRecord):
    codigo: Any = None
    conductor: Any = None
    cel_conductor1: Any = None
    estado_documentos_conductor1: Any = None
    segundo_conductor: Any = None
    cel_conductor2: Any = None
    estado_documentos_conductor2: Any = None
    tercer_conductor: Any = None
    cel_conductor3: Any = None
    estado_documentos_conductor3: Any = None
    contratos_list: Any = None
    estado: Any = None
    fecha_hora_inicio: Any = None
    fecha_hora_fin: Any = None
    responsable_cliente: Any = None
    vehiculo: Any = None

    def to_row(self) -> dict[str, Any]:
        contratos: list[dict[str, Any]] = []
        for item in self.contratos_list or []:
            contrato_id = related_id(item.get("id_contrato") if isinstance(item, dict) else item)
            if contrato_id:
                nombre = related_name(item.get("nombre")) if isinstance(item, dict) else None
                contratos.append({"id_contrato": contrato_id, "nombre": nombre})
        return {
            "codigo": _clean_str(self.codigo),
            "conductor": related_name(self.conductor),
            "cel_conductor1": _clean_str(self.cel_conductor1),
            "estado_documentos_conductor1": _clean_str(self.estado_documentos_conductor1),
            "segundo_conductor": related_name(self.segundo_conductor),
            "cel_conductor2": _clean_str(self.cel_conductor2),
            "estado_documentos_conductor2": _clean_str(self.estado_documentos_conductor2),
            "tercer_conductor": related_name(self.tercer_conductor),
            "cel_conductor3": _clean_str(self.cel_conductor3),
            "estado_documentos_conductor3": _clean_str(self.estado_documentos_conductor3),
            "contratos_list": contratos,
            "estado": _clean_str(self.estado),
            "fecha_hora_inicio": parse_odoo_datetime(self.fecha_hora_inicio),
            "fecha_hora_fin": parse_odoo_datetime(self.fecha_hora_fin),
            "responsable_cliente": related_name(self.responsable_cliente),
            "responsable_cliente_mobile": related_attr(self.responsable_cliente, "mobile"),
            "vehiculo": related_name(self.vehiculo),
            # La API actual solo trae el nombre: el ETL resuelve el id contra `flota` si falta.
            "vehiculo_id": related_id(self.vehiculo),
            "company_id": related_name(self.company_id),
        }


# ─────────────────────────────── Entrada de la Mini App ───────────────────────────────

_REQUIRED_ITEMS = frozenset(item.key for item in CHECKLIST)


FOTO_MAX_BYTES = 3 * 1024 * 1024  # la Mini App comprime a ~300 KB; esto es solo un tope
_FOTO_FIRMAS = {b"\xff\xd8\xff": "image/jpeg", b"\x89PNG\r\n\x1a\n": "image/png"}


def decode_photo(value: str) -> tuple[bytes, str]:
    """Decodifica una foto en base64 (admite prefijo ``data:image/...;base64,``).

    Devuelve (bytes, mime). Valida el tipo por su firma real, no por lo que diga el cliente.
    """
    raw = value.split(",", 1)[1] if value.startswith("data:") else value
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("La foto no es base64 válido") from exc
    if not data:
        raise ValueError("La foto está vacía")
    if len(data) > FOTO_MAX_BYTES:
        raise ValueError(f"La foto supera {FOTO_MAX_BYTES // (1024 * 1024)} MB")
    for firma, mime in _FOTO_FIRMAS.items():
        if data.startswith(firma):
            return data, mime
    raise ValueError("La foto debe ser JPEG o PNG")


class PreoperacionalIn(BaseModel):
    """Formulario enviado por la Mini App. La identidad NO viaja aquí (sale de initData)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # Llave de idempotencia generada en el cliente (crypto.randomUUID()).
    uuid: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    # El vehículo sale del extracto aprobado del conductor. Si la Mini App lo manda,
    # debe coincidir (defensa ante clientes viejos o manipulados).
    extracto_id: int = Field(gt=0, description="id local de la tabla extractos (aprobado y vigente)")
    vehiculo_id: int | None = Field(default=None, gt=0, description="id local de la tabla flota")
    contrato_odoo_id: int | None = Field(default=None, gt=0)
    kilometraje: float = Field(ge=0, le=10_000_000)
    observaciones: str | None = Field(default=None, max_length=2000)
    checklist: dict[str, str]
    llantas: dict[str, float] = Field(default_factory=dict)
    extra: dict[str, str | int | float | bool | None] = Field(default_factory=dict, max_length=50)
    # Foto de los testigos del tablero (obligatoria) -> campo ``testigos_img`` de Odoo.
    testigos_img: str = Field(min_length=16, max_length=FOTO_MAX_BYTES * 4 // 3 + 64)

    @field_validator("testigos_img")
    @classmethod
    def _check_photo(cls, value: str) -> str:
        decode_photo(value)
        return value

    @field_validator("checklist")
    @classmethod
    def _check_items(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = set(value) - _REQUIRED_ITEMS
        if unknown:
            raise ValueError(f"Ítems desconocidos: {', '.join(sorted(unknown))}")
        missing = _REQUIRED_ITEMS - set(value)
        if missing:
            raise ValueError(f"Faltan ítems: {', '.join(sorted(missing))}")
        for key, option in value.items():
            if option not in CHECKLIST_BY_KEY[key].options:
                raise ValueError(f"Valor inválido para {key}: {option!r}")
        return value

    @field_validator("llantas")
    @classmethod
    def _check_measures(cls, value: dict[str, float]) -> dict[str, float]:
        for key, number in value.items():
            spec = MEASURES_BY_KEY.get(key)
            if spec is None:
                raise ValueError(f"Medida desconocida: {key}")
            if not 0 <= number <= spec.max_value:
                raise ValueError(f"{spec.label}: fuera de rango (0–{spec.max_value:g} {spec.unit})")
        return value

    @field_validator("extra")
    @classmethod
    def _check_extra(cls, value: dict[str, Any]) -> dict[str, Any]:
        for key, item in value.items():
            if not key.isidentifier() or len(key) > 64:
                raise ValueError(f"Clave extra inválida: {key!r}")
            if key in _REQUIRED_ITEMS or key in MEASURES_BY_KEY:
                raise ValueError(f"{key} no puede ir en extra")
            if isinstance(item, str) and len(item) > 500:
                raise ValueError(f"extra.{key}: texto demasiado largo")
        return value

    @model_validator(mode="after")
    def _normalize(self) -> PreoperacionalIn:
        self.uuid = self.uuid.lower()
        if self.observaciones == "":
            self.observaciones = None
        return self


class PreoperacionalOut(BaseModel):
    id: int
    uuid: str
    sync_status: str
    odoo_id: int | None
    odoo_name: str | None
    no_conformes: int
    duplicado: bool = False
