"""Catálogo de la Prueba de Ruta: procedimiento, preguntas y fotos (fuente única).

Las claves son los campos del modelo ``prueba.ruta`` de Odoo (``fields_get`` en qa2,
2026-11-25). Cada pregunta es Sí / No y **"Sí" es la novedad** (Odoo deja el registro
*no conforme* con plan de acción). El procedimiento son los 13 pasos que muestra el bot de
referencia (``docs/mapa_viacotur_form.md`` § 3.2).

Decisiones del usuario (2026-11-25): las 6 preguntas son obligatorias y las **dos fotos del
odómetro** (inicial ``km1`` y final ``km2``) también.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

SI = "si"
NO = "no"
ETIQUETAS = {SI: "Sí", NO: "No"}

FOTO_INICIAL = "km1"
FOTO_FINAL = "km2"
FOTOS = {FOTO_INICIAL: "Foto del odómetro inicial", FOTO_FINAL: "Foto del odómetro final"}

PROCEDIMIENTO: tuple[str, ...] = (
    "Ingresar al vehículo.",
    "Ajuste de silla.",
    "Ajuste de cinturón.",
    "Encendido del vehículo.",
    "Girar las ruedas hasta el tope derecho e izquierdo (con los vidrios abajo).",
    "Iniciar la marcha midiendo a qué distancia del pedal de embrague inicia el movimiento el vehículo.",
    "Freno fuerte para detectar sonidos iniciales.",
    "Giro a la izquierda de 45° (escuchar sonidos).",
    "Giro a la derecha de 45° (escuchar sonidos).",
    "En pendiente: frenar a mitad de camino y verificar que el vehículo se sostenga con el pedal presionado.",
    "Arranque en pendiente para evaluar la potencia.",
    "Pendiente de bajada: verificar que el freno sostenga el vehículo.",
    "Finalizar la prueba de ruta (registro fotográfico del odómetro final).",
)


@dataclass(frozen=True)
class Pregunta:
    key: str      # campo del modelo ``prueba.ruta`` en Odoo
    label: str
    corto: str
    bloque: int   # 1 o 2: paso del cuestionario en la Mini App
    obligatorio: bool = True
    options: tuple[str, ...] = (SI, NO)
    alerta: str = SI


PREGUNTAS: tuple[Pregunta, ...] = (
    Pregunta("direccion", "¿Se genera ruido al girar la dirección?", "Ruido al girar la dirección", 1),
    Pregunta("frenos", "¿Se genera ruido en los frenos delanteros?", "Ruido en los frenos delanteros", 1),
    Pregunta("suspencion", "¿Se generan ruidos en la suspensión delantera y en las ruedas?",
             "Ruidos en la suspensión delantera y ruedas", 1),
    Pregunta("pedales", "¿El pedal del freno sufre novedades (se hunde el pedal, el vehículo retrocede o se mueve)?",
             "Novedades en el pedal del freno", 2),
    Pregunta("parqueo",
             "¿La palanca del freno de parqueo sufre novedades (presenta cantidad de clics muy altos, el vehículo "
             "retrocede o se mueve)?",
             "Novedades en el freno de parqueo", 2),
    Pregunta("reductor",
             "¿Cuando el vehículo termina de pasar por el reductor de velocidad, se presenta algún ruido en la "
             "suspensión?",
             "Ruido en la suspensión tras el reductor", 2),
)
PREGUNTAS_BY_KEY: Mapping[str, Pregunta] = {p.key: p for p in PREGUNTAS}


def con_novedad(respuestas: Mapping[str, str]) -> list[Pregunta]:
    """Preguntas respondidas con "Sí" (novedad): Odoo abre plan de acción."""
    return [p for p in PREGUNTAS if respuestas.get(p.key) == p.alerta]


def form_definition() -> dict[str, Any]:
    """Catálogo para la Mini App (``GET /api/prueba-ruta/form``)."""
    return {
        "procedimiento": list(PROCEDIMIENTO),
        "preguntas": [
            {"key": p.key, "label": p.label, "corto": p.corto, "bloque": p.bloque, "alerta": p.alerta,
             "obligatorio": p.obligatorio, "options": [{"value": o, "label": ETIQUETAS[o]} for o in p.options]}
            for p in PREGUNTAS
        ],
        "fotos": [{"key": k, "label": v, "obligatorio": True} for k, v in FOTOS.items()],
        "observaciones_max": 2000,
    }
