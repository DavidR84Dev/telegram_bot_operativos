"""Límites del formulario Pos-Operacional y su definición para la Mini App."""
from __future__ import annotations

from typing import Any

from ..gastos_operativos.catalogo import SOPORTE_MAX_BYTES

KM_MAX = 9_999_999.0
# Salida - Entrada: un turno de más de 24 h es casi seguro un error de fecha (la reserva del vehículo también vence a las 24 h).
RANGO_MAX_HORAS = 24
OBSERVACION_MAX = 1000
PLANILLA_MAX_BYTES = SOPORTE_MAX_BYTES  # 3 MB: el proxy acepta 10 MB por petición y el base64 pesa 4/3
# type_file de Odoo según la firma real del archivo (``gastos_operativos.soporte.formato``).


def form_definition() -> dict[str, Any]:
    return {
        "km_max": KM_MAX,
        "rango_max_horas": RANGO_MAX_HORAS,
        "observacion_max": OBSERVACION_MAX,
        "planilla_max_bytes": PLANILLA_MAX_BYTES,
    }
