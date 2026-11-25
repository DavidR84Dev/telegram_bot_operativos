"""Textos HTML para Telegram."""
from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING

from . import ticket_state

if TYPE_CHECKING:
    from .ast_trabajo.service import AstOutcome, AvisoJefeAst
    from .preop.reservas import Liberacion
    from .preop.service import AvisoJefe, SendOutcome, UbicacionRegistrada


def _route(origin: str | None, destination: str | None) -> str:
    return f"{escape(origin or '—')} → {escape(destination or '—')}"


def ticket_card(ticket: dict) -> str:
    lines = [
        f"🎫 <b>{escape(ticket.get('ticket_number') or '—')}</b>",
        f"👤 Pasajero: {escape(ticket.get('passenger_name') or '—')}",
        f"🚌 Vehículo: {escape(ticket.get('vehicle') or '—')}",
        f"🗺 Ruta 1: {_route(ticket.get('route_origin'), ticket.get('route_destination'))}",
    ]
    if ticket.get("route_origin_2") and ticket.get("route_destination_2"):
        lines.append(
            f"🗺 Ruta 2: {_route(ticket.get('route_origin_2'), ticket.get('route_destination_2'))}"
        )
    lines.append(f"📌 Estado: <b>{escape(ticket_state.label(ticket.get('status')))}</b>")
    return "\n".join(lines)


def boarding_confirmed(ticket: dict) -> str:
    status = ticket.get("status")
    leg = "1ra Ruta" if status == "abordado1" else "2da Ruta"
    pending = ticket_state.has_pending_route_2(ticket) and status == "abordado1"
    tail = "\n⏳ Falta la confirmación de la 2da Ruta." if pending else ""
    return (
        f"✅ <b>Abordaje confirmado ({leg})</b>\n\n"
        f"{ticket_card(ticket)}{tail}"
    )


def preop_result(outcome: SendOutcome) -> str:
    """Aviso al conductor cuando su preoperacional llega a Odoo (o falla definitivamente)."""
    if outcome.status.value == "enviado":
        name = escape(outcome.odoo_name or f"#{outcome.preop_id}")
        text = f"✅ <b>Preoperacional registrado en Odoo</b>\n\n📄 {name}"
        if outcome.no_conformes:
            text += (f"\n⚠️ {outcome.no_conformes} ítem(s) no conforme(s): Odoo abrió "
                     "un plan de acción.")
        return text
    return (
        "❌ <b>Tu preoperacional no pudo registrarse en Odoo</b>\n\n"
        f"Referencia local: #{outcome.preop_id}. El área de sistemas ya tiene el detalle; "
        "no es necesario diligenciarlo de nuevo."
    )


def preop_aviso_jefe(aviso: AvisoJefe, odoo_name: str | None) -> str:
    """Aviso al jefe inmediato cuando un preoperacional de su equipo trae hallazgos."""
    lines = [
        "⚠️ <b>Preoperacional con hallazgos</b>",
        "",
        f"👤 Conductor: <b>{escape(aviso.conductor)}</b>",
        f"🚌 Vehículo: {escape(aviso.vehiculo or '—')}",
    ]
    if odoo_name:
        lines.append(f"📄 {escape(odoo_name)}")
    if aviso.no_conformes:
        lines += ["", f"<b>No conformes ({len(aviso.no_conformes)}):</b>"]
        lines += [f"• {escape(label)}" for label in aviso.no_conformes]
    if aviso.llantas_bajas:
        lines += ["", "<b>Labrado bajo el mínimo del contrato:</b>"]
        lines += [f"• {escape(nombre)}" for nombre in aviso.llantas_bajas]
    return "\n".join(lines)


def ast_result(outcome: AstOutcome) -> str:
    """Aviso al conductor cuando su AST llega a Odoo (o falla definitivamente)."""
    if outcome.status.value == "enviado":
        name = escape(outcome.odoo_name or f"#{outcome.ast_id}")
        text = f"✅ <b>Análisis Seguro de Trabajo registrado en Odoo</b>\n\n📄 {name}"
        if outcome.no_conformes:
            text += (f"\n⚠️ {outcome.no_conformes} respuesta(s) con alerta: Odoo abrió un plan de acción "
                     "y se avisó a tu jefe inmediato.")
        return text
    return (
        "❌ <b>Tu AST no pudo registrarse en Odoo</b>\n\n"
        f"Referencia local: #{outcome.ast_id}. El área de sistemas ya tiene el detalle; "
        "no es necesario diligenciarlo de nuevo."
    )


def ast_aviso_jefe(aviso: AvisoJefeAst, odoo_name: str | None) -> str:
    """Aviso al jefe inmediato cuando un AST de su equipo trae "No conforme" o consumo de medicamentos."""
    lines = [
        "⚠️ <b>AST con alertas</b>",
        "",
        f"👤 Conductor: <b>{escape(aviso.conductor)}</b>",
    ]
    if aviso.contrato:
        lines.append(f"📑 Contrato: {escape(aviso.contrato)}")
    if odoo_name:
        lines.append(f"📄 {escape(odoo_name)}")
    lines += ["", f"<b>Alertas ({len(aviso.alertas)}):</b>"]
    lines += [f"• {escape(label)}" for label in aviso.alertas]
    if aviso.medicamento:
        lines.append(f"💊 Medicamento: {escape(aviso.medicamento)}")
    return "\n".join(lines)


def _lista_placas(placas: tuple[str, ...]) -> str:
    return ", ".join(f"<b>{escape(p)}</b>" for p in placas)


def jornada_finalizada(placas: tuple[str, ...]) -> str:
    if not placas:
        return "No tenías vehículos reservados. Tu jornada quedó finalizada."
    return f"✅ <b>Jornada finalizada.</b>\n\nSe liberó el vehículo {_lista_placas(placas)}. ¡Buen descanso!"


def _hora_local(value) -> str:
    from .preop.reservas import ZONA_JORNADA

    return value.astimezone(ZONA_JORNADA).strftime("%d/%m %I:%M %p") if value else "—"


def jornada_finalizada_jefe(liberacion: Liberacion) -> str:
    """Aviso al jefe inmediato cuando un conductor toca 🔴 Finalizar."""
    return (
        "🔴 <b>Jornada finalizada</b>\n\n"
        f"👤 Conductor: <b>{escape(liberacion.conductor or '—')}</b>\n"
        f"🚌 Vehículo liberado: {_lista_placas(liberacion.placas)}\n"
        f"🕒 Reservado desde: {_hora_local(liberacion.reservado_at)}\n"
        f"🏁 Finalizó: {_hora_local(liberacion.liberado_at)}\n"
        f"📝 Preoperacionales en la jornada: {liberacion.preoperacionales}"
    )


def ubicacion_registrada(registro: UbicacionRegistrada) -> str:
    """Confirmación del botón 📍 Enviar ubicación."""
    text = "📍 <b>Ubicación recibida</b>"
    if registro.preop_id is None:
        return text + "\n\nQuedó registrada. No hay un preoperacional enviado en los últimos 10 minutos para asociarla."
    ref = escape(registro.preop_nombre) if registro.preop_nombre else f"#{registro.preop_id}"
    return text + f"\n\nQuedó asociada a tu preoperacional {ref}."
