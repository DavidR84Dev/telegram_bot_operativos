"""Catálogo del formulario de combustible: fuente única para la Mini App, el esquema y Odoo.

Los tipos de combustible son la ``selection`` del campo ``fuel_type`` del modelo ``fuel`` de
Odoo (``fields_get`` en qa2, 2026-11-25), en el mismo orden.
"""
from __future__ import annotations

from typing import Any

# (valor en Odoo, etiqueta en la Mini App)
TIPOS_COMBUSTIBLE: tuple[tuple[str, str], ...] = (
    ("gasoline", "Gasolina"),
    ("extra", "Gasolina Extra"),
    ("diesel", "Diesel / ACPM"),
    ("electric", "Eléctrico"),
    ("gas", "GNV"),
    ("glp", "GLP"),
    ("diesel urea", "Diesel / Urea"),
    ("hydrogen", "Hidrógeno"),
)
TIPOS_BY_KEY = dict(TIPOS_COMBUSTIBLE)
TIPO_POR_DEFECTO = "diesel"

# Pestañas del bot de referencia -> ``is_fuel_expenses`` de Odoo.
MODALIDADES: tuple[dict[str, Any], ...] = (
    {"key": "combustible", "label": "Combustible", "para_gastos": False},
    {"key": "gastos", "label": "Combustible para gastos", "para_gastos": True},
)

# "Combustible para gastos" va a ``hr.expense`` (POST /api/gastos/register), no al modelo ``fuel``.
PRODUCTO_GASTOS_ODOO_ID = 9629  # product_id por defecto (producción); se configura con COMBUSTIBLE_GASTOS_PRODUCT_ID
NOMBRE_GASTO = "COMBUSTIBLE"
OBSERVACIONES_MAX = 500

# Recordatorios de la ubicación pendiente: minutos desde el envío (máximo uno por cada valor).
RECORDATORIOS_UBICACION_MIN = (10, 30)

# Fecha de la transacción: no futura y como máximo estos días atrás (hora de Colombia).
DIAS_ATRAS_MAX = 30

# Topes de sanidad (no son reglas de negocio; evitan errores de digitación evidentes).
GALONES_MAX = 1000.0
PRECIO_GALON_MAX = 1_000_000.0


def form_definition() -> dict[str, Any]:
    """Parte fija del formulario (la lista de proveedores la agrega el servicio desde la BD)."""
    return {
        "tipos": [{"value": v, "label": label} for v, label in TIPOS_COMBUSTIBLE],
        "tipo_por_defecto": TIPO_POR_DEFECTO,
        "modalidades": list(MODALIDADES),
        "dias_atras_max": DIAS_ATRAS_MAX,
        # La factura (PDF) es obligatoria solo en "Combustible para gastos".
        "factura_obligatoria_en": [m["key"] for m in MODALIDADES if m["para_gastos"]],
    }
