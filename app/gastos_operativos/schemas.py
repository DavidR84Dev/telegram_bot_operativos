"""Esquemas Pydantic de Gastos Operativos: entrada estricta de la Mini App y respuesta."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..combustible.catalogo import DIAS_ATRAS_MAX
from ..preop.reservas import fecha_jornada
from ..preop.schemas import EnvioDiferidoIn
from . import soporte as soporte_mod
from .catalogo import (
    MAX_GASTOS,
    NOMBRE_TIPO_MAX,
    OBSERVACION_MAX,
    SOPORTE_MAX_BYTES,
    SOPORTES_TOTAL_MAX_BYTES,
    VALOR_MAX,
)

_SOPORTE_MAX_B64 = SOPORTE_MAX_BYTES * 4 // 3 + 64


class GastoIn(BaseModel):
    """Una tarjeta «Gasto #N» del formulario."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tipo: str = Field(min_length=1, max_length=NOMBRE_TIPO_MAX)  # nombre del tipo (``GASTOS_OPERATIVOS_TIPOS``)
    valor: int = Field(gt=0, le=VALOR_MAX)  # COP, entero
    observacion: str | None = Field(default=None, max_length=OBSERVACION_MAX)
    soporte: str = Field(min_length=16, max_length=_SOPORTE_MAX_B64)  # PDF o foto JPG/PNG en base64
    soporte_nombre: str | None = Field(default=None, max_length=255)

    @field_validator("valor", mode="before")
    @classmethod
    def _entero(cls, value: Any) -> Any:
        """El valor es un entero en pesos: sin decimales ni separadores (``65000``)."""
        if isinstance(value, bool):
            raise ValueError("El valor debe ser un número entero en pesos")
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError("El valor debe ser un número entero en pesos")
            return int(value)
        if isinstance(value, str):
            texto = value.strip()
            if not texto.isdigit():
                raise ValueError("El valor debe ser un número entero en pesos (solo dígitos)")
            return int(texto)
        return value

    @field_validator("soporte")
    @classmethod
    def _soporte(cls, value: str) -> str:
        soporte_mod.validar(value)  # base64, tamaño y firma (PDF/JPG/PNG)
        return value

    @model_validator(mode="after")
    def _normalizar(self) -> GastoIn:
        self.tipo = " ".join(self.tipo.split())
        if not self.observacion:
            self.observacion = None
        if not self.soporte_nombre:
            self.soporte_nombre = None
        return self


class GastosOperativosIn(BaseModel):
    """Formulario enviado por la Mini App. La identidad NO viaja aquí (sale de initData)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    uuid: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    fecha_gasto: date
    gastos: list[GastoIn] = Field(min_length=1, max_length=MAX_GASTOS)
    diferido: EnvioDiferidoIn | None = None

    @model_validator(mode="after")
    def _normalizar(self) -> GastosOperativosIn:
        self.uuid = self.uuid.lower()
        hoy = fecha_jornada()
        # Abajo, un día de tolerancia: el formulario pudo guardarse sin señal antes de la medianoche.
        if self.fecha_gasto > hoy:
            raise ValueError("La fecha del gasto no puede ser futura")
        if self.fecha_gasto < hoy - timedelta(days=DIAS_ATRAS_MAX + 1):
            raise ValueError(f"La fecha del gasto no puede tener más de {DIAS_ATRAS_MAX} días")
        peso = sum(len(soporte_mod.decodificar(g.soporte)) for g in self.gastos)
        if peso > SOPORTES_TOTAL_MAX_BYTES:
            raise ValueError(f"Los soportes suman más de {SOPORTES_TOTAL_MAX_BYTES // (1024 * 1024)} MB en total")
        return self

    @property
    def total(self) -> int:
        return sum(g.valor for g in self.gastos)


class GastosOperativosOut(BaseModel):
    id: int
    uuid: str
    sync_status: str
    total: float
    gastos: int
    duplicado: bool = False
