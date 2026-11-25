"""Esquemas Pydantic del AST: entrada estricta de la Mini App y respuesta del envío."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .catalogo import MEDICAMENTOS, NOMBRE_MEDICAMENTO_MAX, PREGUNTAS, PREGUNTAS_BY_KEY, SI


class AstIn(BaseModel):
    """Formulario enviado por la Mini App. La identidad NO viaja aquí (sale de initData)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # Llave de idempotencia generada en el cliente (crypto.randomUUID()).
    uuid: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    # Id de `agreement` en Odoo; debe estar en un extracto aprobado del conductor.
    contrato_odoo_id: int | None = Field(default=None, gt=0)
    respuestas: dict[str, str]
    nombre_medicamento: str | None = Field(default=None, max_length=NOMBRE_MEDICAMENTO_MAX)
    observaciones: str | None = Field(default=None, max_length=2000)

    @field_validator("respuestas")
    @classmethod
    def _check_respuestas(cls, value: dict[str, str]) -> dict[str, str]:
        desconocidas = set(value) - set(PREGUNTAS_BY_KEY)
        if desconocidas:
            raise ValueError(f"Preguntas desconocidas: {', '.join(sorted(desconocidas))}")
        # Todas obligatorias (catálogo): la Mini App no deja enviar y el servidor tampoco.
        faltan = [p.corto for p in PREGUNTAS if p.obligatorio and not value.get(p.key)]
        if faltan:
            raise ValueError(f"Responde: {', '.join(faltan)}")
        for key, option in value.items():
            if option not in PREGUNTAS_BY_KEY[key].options:
                raise ValueError(f"Valor inválido para {key}: {option!r}")
        return value

    @model_validator(mode="after")
    def _normalize(self) -> AstIn:
        self.uuid = self.uuid.lower()
        if self.observaciones == "":
            self.observaciones = None
        if self.respuestas.get(MEDICAMENTOS) == SI:
            if not self.nombre_medicamento or len(self.nombre_medicamento) < 3:
                raise ValueError("Indica el nombre del medicamento")
        else:
            # Sin medicamentos el nombre no aplica (pudo quedar escrito antes de cambiar a "No").
            self.nombre_medicamento = None
        return self


class AstOut(BaseModel):
    id: int
    uuid: str
    sync_status: str
    odoo_id: int | None
    odoo_name: str | None
    no_conformes: int
    duplicado: bool = False
