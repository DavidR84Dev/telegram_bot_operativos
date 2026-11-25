"""Esquemas Pydantic del registro de combustible: entrada estricta de la Mini App y respuesta."""
from __future__ import annotations

import base64
import binascii
from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..preop.reservas import fecha_jornada
from ..preop.schemas import EnvioDiferidoIn, _punto_decimal
from .catalogo import DIAS_ATRAS_MAX, GALONES_MAX, PRECIO_GALON_MAX, TIPOS_BY_KEY

FACTURA_MAX_BYTES = 4 * 1024 * 1024  # el proxy (nginx) acepta 10 MB por petición
_FACTURA_MAX_B64 = FACTURA_MAX_BYTES * 4 // 3 + 64


def decode_pdf(value: str) -> bytes:
    """Decodifica la factura en base64 (admite prefijo ``data:application/pdf;base64,``).

    Valida que sea PDF por su firma real (``%PDF-``), no por lo que diga el cliente.
    """
    raw = value.split(",", 1)[1] if value.startswith("data:") else value
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("La factura no es base64 válido") from exc
    if not data:
        raise ValueError("La factura está vacía")
    if len(data) > FACTURA_MAX_BYTES:
        raise ValueError(f"La factura supera {FACTURA_MAX_BYTES // (1024 * 1024)} MB")
    if not data.startswith(b"%PDF-"):
        raise ValueError("La factura debe ser un documento PDF")
    return data


class CombustibleIn(BaseModel):
    """Formulario enviado por la Mini App. La identidad NO viaja aquí (sale de initData)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    uuid: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    # El vehículo es el reservado por el preoperacional; si la Mini App lo manda, debe coincidir.
    vehiculo_id: int | None = Field(default=None, gt=0, description="id local de la tabla flota")
    para_gastos: bool  # pestaña "Combustible para gastos" -> is_fuel_expenses
    # Proveedor: uno de la lista (id local) o escrito a mano (texto -> supplier). Exactamente uno.
    proveedor_id: int | None = Field(default=None, gt=0)
    proveedor_texto: str | None = Field(default=None, max_length=255)
    fecha_transaccion: date
    galones: float = Field(gt=0, le=GALONES_MAX)
    precio_galon: float = Field(gt=0, le=PRECIO_GALON_MAX)
    tipo_combustible: str
    kilometraje: float = Field(ge=0, le=10_000_000)
    ref_factura: str | None = Field(default=None, max_length=128)
    # Factura en PDF (base64). Obligatoria solo con para_gastos.
    factura: str | None = Field(default=None, min_length=16, max_length=_FACTURA_MAX_B64)
    factura_nombre: str | None = Field(default=None, max_length=255)
    diferido: EnvioDiferidoIn | None = None

    @field_validator("galones", "precio_galon", "kilometraje", mode="before")
    @classmethod
    def _decimales(cls, value: Any) -> Any:
        return _punto_decimal(value)

    @field_validator("tipo_combustible")
    @classmethod
    def _tipo(cls, value: str) -> str:
        if value not in TIPOS_BY_KEY:
            raise ValueError(f"Tipo de combustible inválido: {value!r}")
        return value

    @field_validator("factura")
    @classmethod
    def _pdf(cls, value: str | None) -> str | None:
        if value:
            decode_pdf(value)
        return value

    @model_validator(mode="after")
    def _normalize(self) -> CombustibleIn:
        self.uuid = self.uuid.lower()
        for campo in ("proveedor_texto", "ref_factura", "factura_nombre", "factura"):
            if getattr(self, campo) == "":
                setattr(self, campo, None)
        if self.proveedor_texto:
            self.proveedor_texto = " ".join(self.proveedor_texto.split())
        if (self.proveedor_id is None) == (self.proveedor_texto is None):
            raise ValueError("Elige un proveedor de la lista o escribe su nombre")
        if self.para_gastos and not self.factura:
            raise ValueError("Adjunta la factura en PDF: es obligatoria en «Combustible para gastos»")
        hoy = fecha_jornada()
        # Abajo, un día de tolerancia: el formulario pudo guardarse sin señal antes de la medianoche.
        if self.fecha_transaccion > hoy:
            raise ValueError("La fecha de la transacción no puede ser futura")
        if self.fecha_transaccion < hoy - timedelta(days=DIAS_ATRAS_MAX + 1):
            raise ValueError(f"La fecha de la transacción no puede tener más de {DIAS_ATRAS_MAX} días")
        if self.factura and not self.factura_nombre:
            self.factura_nombre = "factura.pdf"
        return self

    @property
    def total(self) -> float:
        return round(self.galones * self.precio_galon, 2)


class CombustibleOut(BaseModel):
    id: int
    uuid: str
    sync_status: str
    odoo_id: int | None
    odoo_name: str | None
    total: float
    duplicado: bool = False
