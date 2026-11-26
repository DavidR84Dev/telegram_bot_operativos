"""Textos HTML para Telegram."""
from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING

from . import ticket_state

if TYPE_CHECKING:
    from .ast_trabajo.service import AstOutcome, AvisoJefeAst
    from .combustible.service import CombustibleOutcome
    from .gastos_operativos.service import GastosOutcome
    from .pos_operacional.service import PosOperacionalOutcome
    from .preop.reservas import Liberacion
    from .prueba_ruta.service import AvisoJefePrueba, PruebaRutaOutcome
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


def prueba_ruta_result(outcome: PruebaRutaOutcome) -> str:
    """Aviso al conductor cuando su prueba de ruta llega a Odoo (o falla definitivamente)."""
    if outcome.status.value == "enviado":
        name = escape(outcome.odoo_name or f"#{outcome.prueba_id}")
        text = f"✅ <b>Prueba de Ruta registrada en Odoo</b>\n\n📄 {name}"
        if outcome.no_conformes:
            text += (f"\n⚠️ {outcome.no_conformes} novedad(es): Odoo abrió un plan de acción "
                     "y se avisó a tu jefe inmediato.")
        return text
    return (
        "❌ <b>Tu Prueba de Ruta no pudo registrarse en Odoo</b>\n\n"
        f"Referencia local: #{outcome.prueba_id}. El área de sistemas ya tiene el detalle; "
        "no es necesario diligenciarla de nuevo."
    )


def prueba_ruta_aviso_jefe(aviso: AvisoJefePrueba, odoo_name: str | None) -> str:
    """Aviso al jefe inmediato cuando una prueba de ruta de su equipo trae novedades."""
    lines = [
        "⚠️ <b>Prueba de Ruta con novedades</b>",
        "",
        f"👤 Conductor: <b>{escape(aviso.conductor)}</b>",
        f"🚌 Vehículo: {escape(aviso.vehiculo or '—')}",
    ]
    if odoo_name:
        lines.append(f"📄 {escape(odoo_name)}")
    lines += ["", f"<b>Novedades ({len(aviso.novedades)}):</b>"]
    lines += [f"• {escape(label)}" for label in aviso.novedades]
    return "\n".join(lines)


def _pesos(valor: float) -> str:
    """126200.5 -> '$ 126 200.50' (punto decimal y miles con espacio, como la Mini App)."""
    entero, _, dec = f"{valor:,.2f}".partition(".")
    texto = entero.replace(",", " ")
    return f"$ {texto}" if dec == "00" else f"$ {texto}.{dec}"


def combustible_result(outcome: CombustibleOutcome) -> str:
    """Aviso al conductor cuando su registro de combustible llega a Odoo (o falla definitivamente)."""
    if outcome.status.value == "enviado":
        return (
            "⛽ <b>Combustible registrado en Odoo</b>\n\n"
            f"🚌 Vehículo: {escape(outcome.placa or '—')}\n"
            f"🛢️ {outcome.galones:g} galones · Total {_pesos(outcome.total)}\n"
            f"📄 Registro #{outcome.odoo_id}"
        )
    return (
        "❌ <b>Tu registro de combustible no pudo guardarse en Odoo</b>\n\n"
        f"Referencia local: #{outcome.combustible_id}. El área de sistemas ya tiene el detalle; "
        "no es necesario diligenciarlo de nuevo."
    )


def combustible_pedir_ubicacion(placa: str | None) -> str:
    return ("⛽ <b>Registro recibido</b>" + (f" ({escape(placa)})" if placa else "") + "\n\n"
            "📍 Toca <b>Enviar ubicación</b> para completar el tanqueo: sin ella el registro no se envía a Odoo.")


def combustible_recordatorio_ubicacion(placa: str | None) -> str:
    return ("⏰ <b>Falta la ubicación de tu tanqueo</b>" + (f" ({escape(placa)})" if placa else "") + "\n\n"
            "Toca <b>Enviar ubicación</b> para completarlo; mientras tanto no se envía a Odoo.")


def combustible_ubicacion_registrada(placa: str | None, pendientes: int) -> str:
    text = "📍 <b>Ubicación registrada para el tanqueo</b>" + (f" de {escape(placa)}" if placa else "") + "."
    if pendientes:
        text += f"\n\nTienes {pendientes} tanqueo(s) más sin ubicación: envíala de nuevo."
    return text


def _plural_gastos(n: int) -> str:
    return f"{n} gasto" if n == 1 else f"{n} gastos"


def gastos_result(outcome: GastosOutcome) -> str:
    """Aviso al conductor con el resultado final de su envío de gastos operativos."""
    if outcome.status.value == "enviado":
        verbo = "registrado" if outcome.enviados == 1 else "registrados"
        return (
            f"💰 <b>{_plural_gastos(outcome.enviados)} {verbo} en Odoo</b>\n\n"
            + (f"🚌 Vehículo: {escape(outcome.placa)}\n" if outcome.placa else "")
            + f"Total {_pesos(outcome.total)}"
        )
    lineas = [f"• Gasto #{f.numero} ({escape(f.tipo)}, {_pesos(f.valor)})" for f in outcome.fallidos]
    verbo = "registrado" if outcome.enviados == 1 else "registrados"
    registrados = (f"✅ {outcome.enviados} de {outcome.total_gastos} gastos {verbo} en Odoo.\n\n"
                   if outcome.enviados else "")
    return (
        "❌ <b>Algunos gastos no pudieron guardarse en Odoo</b>\n\n" + registrados
        + "No se registraron:\n" + "\n".join(lineas) + "\n\n"
        f"Referencia local: #{outcome.gasto_id}. El área de sistemas ya tiene el detalle; "
        "no es necesario diligenciarlos de nuevo."
    )


def gastos_pedir_ubicacion(placa: str | None, gastos: int) -> str:
    return (
        f"💰 <b>{_plural_gastos(gastos)} recibido{'' if gastos == 1 else 's'}</b>"
        + (f" ({escape(placa)})" if placa else "") + "\n\n"
        "📋 <b>Instrucciones para Gastos Operativos:</b>\n"
        "1️⃣ Ya llenaste y enviaste el formulario\n"
        "2️⃣ Toca 📍 <b>Enviar ubicación</b> aquí en el chat\n"
        "⏱️ Tienes <b>10 minutos</b> para enviarla; mientras no lo hagas, tus gastos no se registran en Odoo."
    )


def gastos_recordatorio_ubicacion(placa: str | None) -> str:
    return ("⏰ <b>Falta la ubicación de tus gastos operativos</b>" + (f" ({escape(placa)})" if placa else "")
            + "\n\nToca <b>Enviar ubicación</b> para completarlos; mientras tanto no se registran en Odoo.")


def gastos_rechazado_por_tope(detalle: str) -> str:
    return ("🚫 <b>Tus gastos guardados sin señal no fueron aceptados</b>\n\n" + escape(detalle)
            + "\n\nEl máximo por gasto cambió mientras estabas sin señal. Abre 💰 Gastos Operativos y "
              "diligéncialos de nuevo.")


def gastos_ubicacion_registrada(placa: str | None, pendientes: int) -> str:
    text = "📍 <b>Ubicación registrada para tus gastos operativos</b>" + (f" ({escape(placa)})" if placa else "") + "."
    if pendientes:
        text += f"\n\nTienes {pendientes} registro(s) más sin ubicación: envíala de nuevo."
    return text


POS_RECORDATORIO_FINALIZAR = "Cuando termines tu jornada toca 🔴 <b>Finalizar</b> para liberar el vehículo."


def pos_operacional_recibido(placa: str | None) -> str:
    """Confirmación al recibir el formulario (aún no está en Odoo)."""
    return ("🧾 <b>Pos-operacional recibido</b>" + (f" ({escape(placa)})" if placa else "") + "\n\n"
            "Se está registrando en Odoo; te avisaré el resultado.\n"
            "Enviarlo <b>no libera</b> tu vehículo. " + POS_RECORDATORIO_FINALIZAR)


def pos_operacional_result(outcome: PosOperacionalOutcome) -> str:
    """Resultado final del pos-operacional: registrado en Odoo con su id, o fallido con el motivo."""
    if outcome.status.value == "enviado":
        return (
            "🧾 <b>Pos-operacional registrado en Odoo</b>\n\n"
            + (f"🚌 Vehículo: {escape(outcome.placa)}\n" if outcome.placa else "")
            + f"📄 Registro #{outcome.odoo_id}"
            + (f" ({escape(outcome.odoo_name)})" if outcome.odoo_name else "") + "\n\n"
            + POS_RECORDATORIO_FINALIZAR
        )
    motivo = escape((outcome.error or "motivo no disponible")[:400])
    return (
        "❌ <b>Tu pos-operacional no pudo guardarse en Odoo</b>\n\n"
        f"Motivo: {motivo}\n\n"
        f"Referencia local: #{outcome.pos_id}. El área de sistemas ya tiene el detalle; "
        "no es necesario diligenciarlo de nuevo."
    )


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
