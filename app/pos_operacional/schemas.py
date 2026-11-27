"""Esquemas Pydantic del Post-operacional: entrada estricta de la Mini App y respuesta."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..gastos_operativos import soporte as soporte_mod
from ..preop.reservas import ZONA_JORNADA
from ..preop.schemas import EnvioDiferidoIn, _punto_decimal
from .catalogo import KM_MAX, OBSERVACION_MAX, PLANILLA_MAX_BYTES, RANGO_MAX_HORAS

_PLANILLA_MAX_B64 = PLANILLA_MAX_BYTES * 4 // 3 + 64


class PosOperacionalIn(BaseModel):
    """Formulario enviado por la Mini App. La identidad, el contrato y el vehículo NO viajan aquí (salen del servidor).

    ``entrada`` y ``salida`` son hora de Colombia: sin zona horaria se interpretan como UTC-5 y con zona se convierten.
    Quedan en UTC (``check_in`` / ``check_out`` de Odoo).
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    uuid: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    km_inicial: float = Field(ge=0, le=KM_MAX, allow_inf_nan=False)
    km_final: float = Field(ge=0, le=KM_MAX, allow_inf_nan=False)
    entrada: datetime
    salida: datetime
    planilla: str = Field(min_length=16, max_length=_PLANILLA_MAX_B64)  # PDF, JPG o PNG en base64
    planilla_nombre: str | None = Field(default=None, max_length=255)
    observaciones: str | None = Field(default=None, max_length=OBSERVACION_MAX)
    diferido: EnvioDiferidoIn | None = None

    @field_validator("km_inicial", "km_final", mode="before")
    @classmethod
    def _km_con_punto(cls, value: Any) -> Any:
        # Odoo solo admite punto decimal: "1500,5" (texto de un cliente viejo o de un envío diferido) -> 1500.5.
        if isinstance(value, bool):
            raise ValueError("El kilometraje debe ser un número")
        return _punto_decimal(value)

    @field_validator("planilla")
    @classmethod
    def _planilla(cls, value: str) -> str:
        soporte_mod.validar(value)  # base64, tamaño y firma (PDF/JPG/PNG)
        return value

    @model_validator(mode="after")
    def _normalizar(self) -> PosOperacionalIn:
        self.uuid = self.uuid.lower()
        if not self.observaciones:
            self.observaciones = None
        if not self.planilla_nombre:
            self.planilla_nombre = None
        self.entrada = _a_utc(self.entrada)
        self.salida = _a_utc(self.salida)
        if self.km_final < self.km_inicial:
            raise ValueError("El KM final no puede ser menor que el KM inicial")
        if self.salida <= self.entrada:
            raise ValueError("La salida debe ser posterior a la entrada")
        if self.salida - self.entrada > timedelta(hours=RANGO_MAX_HORAS):
            raise ValueError(f"Entre la entrada y la salida no pueden pasar más de {RANGO_MAX_HORAS} horas")
        return self


def _a_utc(valor: datetime) -> datetime:
    """Hora de Colombia (sin zona = UTC-5) -> UTC con zona, sin segundos fraccionarios."""
    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=ZONA_JORNADA)
    return valor.astimezone(UTC).replace(microsecond=0)


class PosOperacionalOut(BaseModel):
    id: int
    uuid: str
    sync_status: str
    odoo_id: int | None = None
    duplicado: bool = False
