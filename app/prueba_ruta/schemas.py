"""Esquemas Pydantic de la Prueba de Ruta: entrada estricta de la Mini App y respuesta del envío."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..preop.schemas import FOTO_MAX_BYTES, EnvioDiferidoIn, decode_photo
from .catalogo import PREGUNTAS, PREGUNTAS_BY_KEY

_FOTO_MAX_B64 = FOTO_MAX_BYTES * 4 // 3 + 64


class PruebaRutaIn(BaseModel):
    """Formulario enviado por la Mini App. La identidad NO viaja aquí (sale de initData)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    uuid: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    # El vehículo es el reservado por el preoperacional de la jornada. Si la Mini App lo
    # manda, debe coincidir (defensa ante clientes viejos o manipulados).
    vehiculo_id: int | None = Field(default=None, gt=0, description="id local de la tabla flota")
    respuestas: dict[str, str]
    observaciones: str | None = Field(default=None, max_length=2000)
    # Fotos del odómetro (obligatorias): JPEG o PNG en base64 -> km1 / km2 de Odoo.
    km1: str = Field(min_length=16, max_length=_FOTO_MAX_B64)
    km2: str = Field(min_length=16, max_length=_FOTO_MAX_B64)
    # Solo en reenvíos de un formulario guardado sin señal (regla de app/preop/diferido.py).
    diferido: EnvioDiferidoIn | None = None

    @field_validator("km1", "km2")
    @classmethod
    def _check_photo(cls, value: str) -> str:
        decode_photo(value)
        return value

    @field_validator("respuestas")
    @classmethod
    def _check_respuestas(cls, value: dict[str, str]) -> dict[str, str]:
        desconocidas = set(value) - set(PREGUNTAS_BY_KEY)
        if desconocidas:
            raise ValueError(f"Preguntas desconocidas: {', '.join(sorted(desconocidas))}")
        faltan = [p.corto for p in PREGUNTAS if p.obligatorio and not value.get(p.key)]
        if faltan:
            raise ValueError(f"Responde: {', '.join(faltan)}")
        for key, option in value.items():
            if option not in PREGUNTAS_BY_KEY[key].options:
                raise ValueError(f"Valor inválido para {key}: {option!r}")
        return value

    @model_validator(mode="after")
    def _normalize(self) -> PruebaRutaIn:
        self.uuid = self.uuid.lower()
        if self.observaciones == "":
            self.observaciones = None
        return self


class PruebaRutaOut(BaseModel):
    id: int
    uuid: str
    sync_status: str
    odoo_id: int | None
    odoo_name: str | None
    no_conformes: int
    duplicado: bool = False
