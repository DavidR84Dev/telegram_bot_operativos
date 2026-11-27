"""Esquemas Pydantic de Correcciones: entrada estricta de la Mini App (un cuerpo por tipo) y respuesta."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..gastos_operativos import soporte as soporte_mod
from ..pos_operacional.schemas import _a_utc
from ..preop.reservas import fecha_jornada
from ..preop.schemas import EnvioDiferidoIn, _punto_decimal
from .catalogo import KM_MAX, PDF_MAX_BYTES, RANGO_MAX_HORAS, TEXTO_MAX, VALOR_MAX, dias_maximos

_PDF_MAX_B64 = PDF_MAX_BYTES * 4 // 3 + 64


def _validar_pdf(value: str) -> str:
    """PDF o foto JPG/PNG (por su firma, no por lo que diga el cliente) y dentro del tamaño máximo.

    Las fotos se convierten a PDF al recibir el envío (``service.submit``): a Odoo siempre llega un PDF.
    """
    soporte_mod.validar(value)  # base64, tamaño y firma (PDF/JPG/PNG)
    return value


class _EnvioBase(BaseModel):
    """Campos comunes a todo envío. La identidad NO viaja aquí (sale de ``initData``)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    uuid: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    diferido: EnvioDiferidoIn | None = None

    @model_validator(mode="after")
    def _normalizar_base(self) -> _EnvioBase:
        self.uuid = self.uuid.lower()
        return self


class _CorreccionBase(_EnvioBase):
    odoo_id: int = Field(gt=0)  # id del hr.expense rechazado / del pos.operacional pendiente que se corrige


class CorreccionGastoIn(_CorreccionBase):
    """Corrección de un gasto rechazado. Nombre, tipo y empleado se conservan del original (salen de Odoo)."""

    tipo: Literal["gasto"]
    total: float = Field(gt=0, le=VALOR_MAX, allow_inf_nan=False)
    fecha: date
    descripcion: str = Field(min_length=1, max_length=TEXTO_MAX)  # explica la corrección: obligatoria
    pdf: str = Field(min_length=16, max_length=_PDF_MAX_B64)      # soporte nuevo (PDF o foto): obligatorio
    pdf_nombre: str | None = Field(default=None, max_length=255)

    @field_validator("total", mode="before")
    @classmethod
    def _total_con_punto(cls, value: Any) -> Any:
        # Odoo solo admite punto decimal: "65000,5" (texto de un cliente viejo o de un envío diferido) -> 65000.5.
        if isinstance(value, bool):
            raise ValueError("El valor debe ser un número")
        return _punto_decimal(value)

    @field_validator("total")
    @classmethod
    def _dos_decimales(cls, value: float) -> float:
        if round(value, 2) != value:
            raise ValueError("El valor admite como máximo 2 decimales")
        return value

    @field_validator("pdf")
    @classmethod
    def _pdf(cls, value: str) -> str:
        return _validar_pdf(value)

    @model_validator(mode="after")
    def _normalizar(self) -> CorreccionGastoIn:
        if not self.pdf_nombre:
            self.pdf_nombre = None
        hoy = fecha_jornada()
        if self.fecha > hoy:
            raise ValueError("La fecha del gasto no puede ser futura")
        dias = dias_maximos()
        if self.fecha < hoy - timedelta(days=dias):
            raise ValueError(f"La fecha del gasto no puede tener más de {dias} días de antigüedad")
        return self


class CorreccionPosIn(_CorreccionBase):
    """Corrección de un Post-operacional pendiente. ``entrada`` y ``salida`` son hora de Colombia (como en el Pos)."""

    tipo: Literal["pos"]
    km_inicial: float = Field(ge=0, le=KM_MAX, allow_inf_nan=False)
    km_final: float = Field(ge=0, le=KM_MAX, allow_inf_nan=False)
    entrada: datetime
    salida: datetime
    observaciones: str = Field(min_length=1, max_length=TEXTO_MAX)  # explica la corrección: obligatoria
    planilla: str | None = Field(default=None, min_length=16, max_length=_PDF_MAX_B64)  # PDF o foto, opcional: si falta se conserva
    planilla_nombre: str | None = Field(default=None, max_length=255)

    @field_validator("km_inicial", "km_final", mode="before")
    @classmethod
    def _km_con_punto(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("El kilometraje debe ser un número")
        return _punto_decimal(value)

    @field_validator("planilla")
    @classmethod
    def _planilla(cls, value: str | None) -> str | None:
        return _validar_pdf(value) if value is not None else None

    @model_validator(mode="after")
    def _normalizar(self) -> CorreccionPosIn:
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


class CorreccionPlanillaIn(_EnvioBase):
    """Carga de una planilla no enviada: crea un Post-operacional nuevo. ``fecha`` es el día (Colombia) de la jornada
    y ``entrada`` la hora de entrada (Colombia), que debe caer en ese día y no ser futura."""

    tipo: Literal["planilla"]
    fecha: date
    entrada: datetime
    observaciones: str = Field(min_length=1, max_length=TEXTO_MAX)  # obligatorias
    planilla: str = Field(min_length=16, max_length=_PDF_MAX_B64)   # PDF o foto: obligatoria
    planilla_nombre: str | None = Field(default=None, max_length=255)

    @field_validator("planilla")
    @classmethod
    def _planilla(cls, value: str) -> str:
        return _validar_pdf(value)

    @model_validator(mode="after")
    def _normalizar(self) -> CorreccionPlanillaIn:
        if not self.planilla_nombre:
            self.planilla_nombre = None
        self.entrada = _a_utc(self.entrada)
        hoy = fecha_jornada()
        if self.fecha > hoy:
            raise ValueError("La fecha de la jornada no puede ser futura")
        dias = dias_maximos()
        if self.fecha < hoy - timedelta(days=dias):
            raise ValueError(f"La jornada no puede tener más de {dias} días de antigüedad")
        if fecha_jornada(self.entrada) != self.fecha:
            raise ValueError("La hora de entrada debe estar dentro del día de la jornada")
        if self.entrada > datetime.now(UTC):
            raise ValueError("La hora de entrada no puede ser futura")
        return self


CorreccionIn = Annotated[CorreccionGastoIn | CorreccionPosIn | CorreccionPlanillaIn, Field(discriminator="tipo")]


class CorreccionOut(BaseModel):
    id: int
    uuid: str
    tipo: str
    sync_status: str
    duplicado: bool = False
