"""Catálogo del AST: fuente única de las preguntas, sus opciones y su campo en Odoo.

Las claves son los nombres técnicos del modelo ``ast`` de Odoo (verificados con
``fields_get`` en qa2, 2026-11-25), así que el mapeo es directo. La Mini App recibe
este catálogo por ``GET /api/ast/form`` y no repite la lista.

Decisiones del usuario (2026-11-25):

* Se preguntan las **10** preguntas de Odoo, incluidas ``documentos`` y ``descanso``,
  que el bot de referencia (@viacoturform_bot) no muestra.
* Todas son **obligatorias**. Se permite enviar con "No conforme" o medicamentos = "Sí"
  (Odoo abre el plan de acción) y el bot avisa al jefe inmediato.
* Con medicamentos = "Sí" el nombre del medicamento es obligatorio.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

CONFORME = "conforme"
NO_CONFORME = "no conforme"
SI = "si"
NO = "no"

OPCIONES_CONFORMIDAD = (CONFORME, NO_CONFORME)
OPCIONES_SI_NO = (SI, NO)
ETIQUETAS = {CONFORME: "Conforme", NO_CONFORME: "No conforme", SI: "Sí", NO: "No"}

MEDICAMENTOS = "medicamentos"
NOMBRE_MEDICAMENTO_MAX = 120


@dataclass(frozen=True)
class Pregunta:
    key: str                 # campo del modelo ``ast`` en Odoo
    label: str               # texto completo de la pregunta
    corto: str               # texto corto para el resumen y el aviso al jefe
    options: tuple[str, ...]
    alerta: str              # respuesta que abre plan de acción en Odoo
    obligatorio: bool = True


# Orden: primero las dos que solo están en Odoo (mismo orden de su vista), luego las del bot
# de referencia y al final la de medicamentos (tarjeta amarilla en la referencia).
PREGUNTAS: tuple[Pregunta, ...] = (
    Pregunta("documentos",
             "¿Los documentos como licencia, autorizaciones y demás documentos para la operación del "
             "vehículo están vigentes?",
             "Documentos vigentes", OPCIONES_CONFORMIDAD, NO_CONFORME),
    Pregunta("descanso", "¿Descansó adecuadamente?", "Descanso adecuado", OPCIONES_CONFORMIDAD, NO_CONFORME),
    Pregunta("condiciones",
             "¿Está en condiciones físicas y psicológicas para ejecutar la tarea sin fatiga, trastorno del "
             "sueño, problemas personales críticos u otra condición de salud que ponga en riesgo su integridad "
             "y la de los demás en la vía?",
             "Condiciones físicas y psicológicas", OPCIONES_CONFORMIDAD, NO_CONFORME),
    Pregunta("epp",
             "¿Cuenta con los EPP (elementos de protección personal) y están en perfectas condiciones para "
             "cumplir con el desarrollo de la operación?",
             "Elementos de protección personal (EPP)", OPCIONES_CONFORMIDAD, NO_CONFORME),
    Pregunta("peligros",
             "¿Conoce, identifica los peligros y riesgos a los que se encuentra expuesto y aplica las medidas "
             "de intervención establecidas?",
             "Peligros y riesgos", OPCIONES_CONFORMIDAD, NO_CONFORME),
    Pregunta("pausas",
             "¿Es consciente de que debe realizar pausas activas y con mayor énfasis en recorridos de más de "
             "2 horas de conducción?",
             "Pausas activas", OPCIONES_CONFORMIDAD, NO_CONFORME),
    Pregunta("procedimientos",
             "¿Conoce los procedimientos a seguir en caso de accidente de tránsito y accidente laboral?",
             "Procedimientos en caso de accidente", OPCIONES_CONFORMIDAD, NO_CONFORME),
    Pregunta("aspectos",
             "¿Conoce, identifica los aspectos e impactos ambientales y aplica las medidas de control "
             "establecidas?",
             "Aspectos e impactos ambientales", OPCIONES_CONFORMIDAD, NO_CONFORME),
    Pregunta("conservacion",
             "¿Es consciente de la conservación del medio ambiente y los recursos naturales, siguiendo las "
             "recomendaciones del Sistema de Gestión Ambiental?",
             "Conservación del medio ambiente", OPCIONES_CONFORMIDAD, NO_CONFORME),
    Pregunta(MEDICAMENTOS,
             "¿Ha consumido medicamentos que puedan generar somnolencia, disminución de reflejos u otros "
             "efectos secundarios que afecten la seguridad en la operación? En caso afirmativo, indique el "
             "nombre del medicamento.",
             "Consumo de medicamentos", OPCIONES_SI_NO, SI),
)
PREGUNTAS_BY_KEY: Mapping[str, Pregunta] = {p.key: p for p in PREGUNTAS}


def con_alerta(respuestas: Mapping[str, str]) -> list[Pregunta]:
    """Preguntas cuya respuesta abre plan de acción en Odoo ("No conforme" o medicamentos = "Sí")."""
    return [p for p in PREGUNTAS if respuestas.get(p.key) == p.alerta]


def form_definition() -> dict[str, Any]:
    """Catálogo para la Mini App (``GET /api/ast/form``)."""
    return {
        "preguntas": [
            {"key": p.key, "label": p.label, "corto": p.corto, "alerta": p.alerta, "obligatorio": p.obligatorio,
             "options": [{"value": o, "label": ETIQUETAS[o]} for o in p.options]}
            for p in PREGUNTAS
        ],
        "medicamento": {"pregunta": MEDICAMENTOS, "si": SI, "max": NOMBRE_MEDICAMENTO_MAX},
        "observaciones_max": 2000,
    }
