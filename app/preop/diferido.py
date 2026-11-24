"""Regla del envío diferido: formularios guardados en el teléfono por falta de señal.

Decisión del 2026-11-24: un formulario que no se pudo enviar por falta de señal se
reenvía **como máximo 1 hora después de perder la señal**, para no chocar con el resto
de la lógica (reserva del vehículo, odómetro, ubicación, sincronización con Odoo).
Fuera de esa ventana queda **vencido**: se guarda solo como auditoría en
``preop_envios_diferidos``, no se envía a Odoo, no reserva el vehículo y no se notifica
al conductor.

La hora de pérdida de señal la marca el teléfono (su reloj se puede cambiar). Por eso
se acota con la hora de apertura del formulario, que va firmada por Telegram
(``auth_date`` del initData original):

1. apertura − tolerancia ≤ sin_señal_desde ≤ recepción + tolerancia (reloj coherente)
2. recepción − sin_señal_desde ≤ 1 h (+ tolerancia)
3. recepción − apertura ≤ 2 h (1 h para diligenciar + 1 h sin señal), tope no falsificable
"""
from __future__ import annotations

from datetime import datetime, timedelta

VENTANA_SIN_SENAL = timedelta(hours=1)
TOPE_DESDE_APERTURA = timedelta(hours=2)
# Diferencia tolerada entre el reloj del teléfono y el del servidor.
TOLERANCIA_RELOJ = timedelta(minutes=2)
# Antigüedad máxima del initData original para dejar siquiera el registro de auditoría.
ANTIGUEDAD_MAXIMA_AUDITORIA = timedelta(days=7)


def motivo_vencido(apertura: datetime, sin_senal_desde: datetime, recibido: datetime) -> str | None:
    """None si el envío diferido está dentro de la ventana; si no, el motivo (para auditoría)."""
    if sin_senal_desde < apertura - TOLERANCIA_RELOJ:
        return "Reloj del teléfono incoherente: pérdida de señal anterior a la apertura del formulario"
    if sin_senal_desde > recibido + TOLERANCIA_RELOJ:
        return "Reloj del teléfono incoherente: pérdida de señal posterior a la recepción"
    if recibido - sin_senal_desde > VENTANA_SIN_SENAL + TOLERANCIA_RELOJ:
        minutos = int((recibido - sin_senal_desde).total_seconds() // 60)
        return f"Llegó {minutos} min después de perder la señal (máximo 60 min)"
    if recibido - apertura > TOPE_DESDE_APERTURA:
        minutos = int((recibido - apertura).total_seconds() // 60)
        return f"Llegó {minutos} min después de abrir el formulario (máximo 120 min)"
    return None
