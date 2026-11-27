"""Límites del formulario de Correcciones y su definición para la Mini App."""
from __future__ import annotations

from typing import Any

from ..gastos_operativos.catalogo import SOPORTE_MAX_BYTES, VALOR_MAX
from ..pos_operacional.catalogo import KM_MAX, RANGO_MAX_HORAS

TIPO_GASTO = "gasto"
TIPO_POS = "pos"
TIPO_PLANILLA = "planilla"  # carga de una planilla no enviada: crea un pos.operacional nuevo (POST /api/posoperacional/register)
PLANILLA_PATH = "/api/posoperacional/register"
ENVIADA_HOLGURA_MIN = 2  # una corrección enviada sigue oculta mientras Odoo no cambie el registro después de enviado_at + 2 min
TEXTO_MAX = 1000          # descripción del gasto / observaciones del Post-operacional
PDF_MAX_BYTES = SOPORTE_MAX_BYTES  # mismo máximo que Gastos Operativos (3 MB)
DIAS_POR_DEFECTO = 30      # antigüedad máxima por defecto (CORRECCIONES_DIAS_MAXIMOS)
DIAS_MIN_LIMITE = 1
DIAS_MAX_LIMITE = 365
SEPARADOR_OBSERVACION = "[Corrección {fecha}] "  # encabezado del bloque que se agrega a las observaciones del Pos


def dias_maximos(valor: Any = None) -> int:
    """Antigüedad máxima en días: ``valor`` o ``CORRECCIONES_DIAS_MAXIMOS``, acotada a 1..365."""
    if valor is None:
        from ..config import get_settings

        valor = get_settings().correcciones_dias_maximos
    return max(DIAS_MIN_LIMITE, min(DIAS_MAX_LIMITE, int(valor)))

# Estados de Odoo que intervienen (ver docs/CORRECCIONES.md).
GASTO_RECHAZADO = "refused"
GASTO_CORRECCION = "correction"
POS_PENDIENTE_GASTOS = "refused"
POS_PENDIENTE_NOMINA = "refused by"
POS_CORRECCION_GASTOS = "correct"
POS_CORRECCION_NOMINA = "correct by"
ESTADOS_POS_PENDIENTES = (POS_PENDIENTE_GASTOS, POS_PENDIENTE_NOMINA, POS_CORRECCION_NOMINA)
ESTADOS_POS_CORREGIDOS = (POS_CORRECCION_GASTOS, POS_CORRECCION_NOMINA)
# Estado al que pasa cada Pos al corregirlo.
# Un Pos en «correct by» (nómina pidió otra corrección) se vuelve a corregir y sigue en «correct by».
POS_ESTADO_CORREGIDO = {POS_PENDIENTE_GASTOS: POS_CORRECCION_GASTOS, POS_PENDIENTE_NOMINA: POS_CORRECCION_NOMINA,
                        POS_CORRECCION_NOMINA: POS_CORRECCION_NOMINA}
POS_ESTADO_TEXTO = {
    POS_PENDIENTE_GASTOS: "PENDIENTE GASTOS", POS_PENDIENTE_NOMINA: "PENDIENTE NÓMINA",
    POS_CORRECCION_GASTOS: "CORRECCIÓN GASTOS", POS_CORRECCION_NOMINA: "CORRECCIÓN NÓMINA",
    "draft": "BORRADOR", "approved": "APROBADO", "approved by": "APROBADO NÓMINA", "done": "HECHO",
    "cancelled": "CANCELADO",
}


def form_definition() -> dict[str, Any]:
    return {
        "valor_max": VALOR_MAX,
        "km_max": KM_MAX,
        "rango_max_horas": RANGO_MAX_HORAS,
        "texto_max": TEXTO_MAX,
        "pdf_max_bytes": PDF_MAX_BYTES,
        "dias_maximos": dias_maximos(),
    }
