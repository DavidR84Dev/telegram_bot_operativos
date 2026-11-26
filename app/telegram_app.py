"""Handlers de @OperativosViacoturBot: menú principal, formularios (Mini Apps), ubicación y aviso de abordaje.

El bot es el punto de entrada de las funcionalidades operativas. Como el bot de
referencia (@viacoturform_bot), el teclado de respuesta es plegable: Telegram lo
muestra detrás de su botón de teclado (``toggle-reply-markup``) y solo tiene:

    📋 Formularios      -> panel inline, en este orden:
                             • formularios de ``FORMULARIOS`` (✅ Preoperacional, ⚠️ AST, 🗺️ Prueba de Ruta)
                             • 📷 Escanear QR (Mini App del escáner)
                             • 🔴 Finalizar   (fin de jornada: libera el vehículo reservado)
    📍 Enviar ubicación -> comparte la ubicación (se guarda en la BD del bot)

Un usuario sin acceso (su id de Telegram no está en el PIN de ningún empleado) ve en su
lugar 📝 Registro: tres preguntas (nombre, documento, correo) que se guardan en
``registros_conductor``, se validan en Odoo y registran su id de Telegram como PIN
(``preop.registro``). Quien ya completó el registro no vuelve a ver esa opción.

Agregar un formulario nuevo = agregar una ``MenuOption`` a ``FORMULARIOS``: aparece
solo en el panel, antes de Escanear QR y Finalizar.

La lectura del QR la hace el escáner nativo de Telegram dentro de la Mini App
(``/scanner``). En el chat solo se acepta pegar la URL del QR como respaldo.
No se aceptan fotos.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable
from html import escape

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from . import formatting, ticket_state
from .access import AccessControl
from .config import Settings
from .odoo_client import OdooClient
from .odoo_queries import find_employee_by_pin, find_internal_token, find_tickets_by_ids
from .passenger_notifier import PassengerNotifier
from .preop import registro as preop_registro
from .preop import reservas as preop_reservas
from .combustible import service as combustible_service
from .preop import service as preop_service
from .preop.rest_client import OdooRestClient
from .scan_service import STATUS_MESSAGES, scan_qr
from .watch_store import WatchStore
from .watcher import Watcher, utc_now

logger = logging.getLogger(__name__)

SERVICES_KEY = "services"
# BD del preoperacional (la cuelga main._start_preop); la usa el handler de ubicación.
PREOP_DB_KEY = "preop_db"
BTN_FORMULARIOS = "📋 Formularios"
BTN_ESCANEAR = "📷 Escanear QR Tickets"
BTN_UBICACION = "📍 Enviar ubicación"
BTN_PREOP = "✅ Preoperacional"
BTN_AST = "⚠️ AST"
BTN_PRUEBA_RUTA = "🗺️ Prueba de Ruta"
BTN_COMBUSTIBLE = "⛽ Combustible"
BTN_FINALIZAR = "🔴 Finalizar"
CB_FINALIZAR_SI = "finalizar:si"
CB_FINALIZAR_NO = "finalizar:no"
CB_FINALIZAR_PEDIR = "finalizar:pedir"  # botón 🔴 Finalizar del panel de formularios
FINALIZAR_PREGUNTA = "¿Confirmas que ya terminaste tu jornada laboral?"
BTN_FINALIZAR_SI = "✅ Sí, finalizar"
BTN_FINALIZAR_NO = "❌ Cancelar"

# ── 📝 Registro de conductores nuevos ──
REGISTRO_KEY = "registro"
BTN_REGISTRO = "📝 Registro"
REG_NOMBRE, REG_DOCUMENTO, REG_CORREO = range(3)
MSG_REG_INVITACION = (
    "👋 Hola, aún no estás registrado en el bot <b>Operativos-Viacotur</b>.\n\n"
    f"Toca <b>{BTN_REGISTRO}</b> para registrarte. Te pediré tu nombre completo, tu número "
    "de documento y tu correo electrónico."
)
MSG_REG_NOMBRE = "¡Hola! Para registrarte necesito tu <b>nombre completo</b>:"
MSG_REG_DOCUMENTO = "Gracias, {nombre}. Ahora tu <b>número de cédula o documento de identidad</b> (solo números):"
MSG_REG_CORREO = "Por último, tu <b>correo electrónico</b>:"
MSG_REG_ERROR_NOMBRE = "Escribe tu nombre y apellidos, solo con letras (por ejemplo: Ana María Pérez Gómez)."
MSG_REG_ERROR_DOCUMENTO = "Ese documento no parece válido. Envíalo solo con números, sin puntos ni espacios."
MSG_REG_ERROR_CORREO = "Ese correo no parece válido. Escríbelo completo, por ejemplo: nombre@correo.com"
MSG_REG_VALIDANDO = "⏳ Validando tus datos en Odoo…"
MSG_REG_NO_ENCONTRADO = "Conductor no registrado en Odoo. Por favor comunicarse con el área de Talento humano"
MSG_REG_EXITO = ("Usuario correctamente registrado en Odoo. Bienvenido, ya puedes hacer uso del Bot "
                 "'Operativos-Viacotur'")
MSG_REG_PIN_OCUPADO = ("Ese documento ya está asociado a otra cuenta de Telegram. Por favor comunicarse "
                       "con el área de Talento humano")
MSG_REG_ERROR_ODOO = ("No fue posible validar tus datos en Odoo en este momento. Intenta de nuevo en unos "
                      f"minutos con <b>{BTN_REGISTRO}</b>.")
MSG_REG_YA_REGISTRADO = "Ya estás registrado. Usa el menú para continuar."
MSG_REG_SIN_ACCESO = ("🔒 Tu registro ya fue realizado, pero tu usuario no tiene acceso en Odoo. Por favor "
                      "comunicarse con el área de Talento humano")
MSG_REG_NO_DISPONIBLE = "El registro no está disponible en este momento. Intenta más tarde."
MSG_REG_CANCELADO = f"Registro cancelado. Cuando quieras empezar de nuevo toca <b>{BTN_REGISTRO}</b>."


@dataclass(frozen=True)
class MenuOption:
    """Opción del menú principal: botón de teclado -> botón inline que abre la Mini App.

    Las Mini Apps se abren con botones *inline*: las lanzadas desde un botón de
    teclado llegan sin ``initData`` y no se pueden autenticar.
    """

    label: str                          # nombre de la opción (botón de la lista o del teclado)
    open_text: str                      # texto del botón inline que abre la Mini App
    prompt: str                         # mensaje que acompaña al botón inline
    help: str                           # línea de ayuda (HTML)
    url: Callable[[Settings], str]      # URL HTTPS de la Mini App


@dataclass
class Services:
    settings: Settings
    odoo: OdooClient
    access: AccessControl
    store: WatchStore
    watcher: Watcher


@dataclass
class RegistroServices:
    """Lo que necesita 📝 Registro: BD del bot y cliente de la API REST de Odoo (lo arma main)."""

    db: object                                   # preop.db.Database
    client_factory: Callable[[], OdooRestClient]  # un cliente nuevo por registro (se cierra al terminar)
    company_ids: list[int]


BOT_COMMANDS = [
    BotCommand("start", "Abrir el menú principal"),
    BotCommand("registro", "Registrarte como conductor en Viacotur"),
    BotCommand("formularios", "Ver los formularios disponibles"),
    BotCommand("preoperacional", "Diligenciar la inspección preoperacional"),
    BotCommand("ast", "Diligenciar el Análisis Seguro de Trabajo (AST)"),
    BotCommand("prueba_ruta", "Diligenciar la Prueba de Ruta del vehículo"),
    BotCommand("combustible", "Registrar un tanqueo de combustible"),
    BotCommand("escanear", "Abrir el escáner de QR"),
    BotCommand("finalizar", "Finalizar la jornada y liberar el vehículo reservado"),
    BotCommand("ayuda", "Qué hace cada opción del menú"),
]


def scanner_url(base: str) -> str:
    return f"{base.rstrip('/')}/scanner"


def preop_url(settings: Settings) -> str:
    base = settings.preop_base_url or settings.scanner_base_url
    return f"{base.rstrip('/')}/preoperacional"


def ast_url(settings: Settings) -> str:
    # Lo sirve este mismo servicio, junto al preoperacional.
    base = settings.preop_base_url or settings.scanner_base_url
    return f"{base.rstrip('/')}/ast"


def prueba_ruta_url(settings: Settings) -> str:
    base = settings.preop_base_url or settings.scanner_base_url
    return f"{base.rstrip('/')}/prueba-ruta"


def combustible_url(settings: Settings) -> str:
    base = settings.preop_base_url or settings.scanner_base_url
    return f"{base.rstrip('/')}/combustible"


def scanner_inline(base: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📷 Abrir escáner", web_app=WebAppInfo(url=scanner_url(base)))]]
    )


# Formularios disponibles: orden de la lista inline = orden de esta tupla.
FORMULARIOS: tuple[MenuOption, ...] = (
    MenuOption(
        label=BTN_PREOP,
        open_text=BTN_PREOP,
        prompt="Toca para abrir el formulario preoperacional:",
        help="inspección preoperacional del vehículo antes de salir.",
        url=preop_url,
    ),
    MenuOption(
        label=BTN_AST,
        open_text=BTN_AST,
        prompt="Toca para abrir el Análisis Seguro de Trabajo:",
        help="Análisis Seguro de Trabajo: verifica tus condiciones de seguridad antes de la operación.",
        url=ast_url,
    ),
    MenuOption(
        label=BTN_PRUEBA_RUTA,
        open_text=BTN_PRUEBA_RUTA,
        prompt="Toca para abrir la Prueba de Ruta:",
        help=("prueba de ruta del vehículo reservado con tu preoperacional (fotos del odómetro inicial y final). "
              "Solo para contratos autorizados."),
        url=prueba_ruta_url,
    ),
    MenuOption(
        label=BTN_COMBUSTIBLE,
        open_text=BTN_COMBUSTIBLE,
        prompt="Toca para registrar el combustible:",
        help=("registro de tanqueo del vehículo reservado con tu preoperacional (galones, precio, proveedor, "
              "kilometraje y factura en PDF)."),
        url=combustible_url,
    ),
)
_FORM_BY_LABEL = {option.label: option for option in FORMULARIOS}

ESCANER = MenuOption(
    label=BTN_ESCANEAR,
    open_text=BTN_ESCANEAR,
    prompt="Toca para abrir el escáner:",
    help=("lee el QR del pasajero, muestra la ficha del ticket y el botón de la página de abordaje. "
          "Cuando se registre el abordaje recibirás aquí la confirmación."),
    url=lambda settings: scanner_url(settings.scanner_base_url),
)

HELP_TEXT = (
    "Bienvenido 👋 Elige una opción:\n\n"
    f"<b>{BTN_FORMULARIOS}</b>:\n"
    + "".join(f"   • <b>{f.label}</b>: {f.help}\n" for f in FORMULARIOS)
    + f"   • <b>{BTN_ESCANEAR}</b>: {ESCANER.help}\n"
    f"   • <b>{BTN_FINALIZAR}</b>: termina tu jornada y libera el vehículo que quedó reservado a tu nombre "
    "con el preoperacional, para que otro conductor pueda usarlo.\n\n"
    f"<b>{BTN_UBICACION}</b>: comparte tu ubicación actual. Si acabas de enviar un preoperacional "
    "(últimos 10 minutos), queda asociada a él.\n\n"
    "El menú se abre y se cierra con el botón de teclado ⌨️ junto al campo de mensaje."
)


def main_keyboard() -> ReplyKeyboardMarkup:
    """Teclado del menú principal: solo Formularios y Enviar ubicación.

    No es persistente (``is_persistent=False``): Telegram lo pliega detrás de su botón de
    teclado (``toggle-reply-markup``), igual que el bot de referencia.
    """
    return ReplyKeyboardMarkup(
        [
         [KeyboardButton(BTN_FORMULARIOS), KeyboardButton(BTN_UBICACION, request_location=True)],
        ],
        resize_keyboard=True,
        is_persistent=False,
        input_field_placeholder="Elige una opción del menú",
    )


def ubicacion_keyboard() -> ReplyKeyboardMarkup:
    """Teclado con un solo botón 📍 que comparte la ubicación (tanqueo pendiente de ubicación)."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BTN_UBICACION, request_location=True)]],
        resize_keyboard=True, one_time_keyboard=True, is_persistent=False,
        input_field_placeholder="Toca 📍 Enviar ubicación",
    )


def registro_keyboard() -> ReplyKeyboardMarkup:
    """Teclado de los usuarios sin registrar: solo 📝 Registro."""
    return ReplyKeyboardMarkup([[KeyboardButton(BTN_REGISTRO)]], resize_keyboard=True,
                               input_field_placeholder="Toca 📝 Registro")


def option_inline(option: MenuOption, settings: Settings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(option.open_text, web_app=WebAppInfo(url=option.url(settings)))]])


def formularios_inline(settings: Settings) -> InlineKeyboardMarkup:
    """Panel de 📋 Formularios: cada formulario, luego 📷 Escanear QR y al final 🔴 Finalizar.

    Formularios y escáner son botones inline ``web_app`` (traen initData). Finalizar es un
    botón de callback que pide la confirmación de fin de jornada.
    """
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f.open_text, web_app=WebAppInfo(url=f.url(settings)))] for f in FORMULARIOS]
        + [[InlineKeyboardButton(ESCANER.open_text, web_app=WebAppInfo(url=ESCANER.url(settings)))]]
        + [[InlineKeyboardButton(BTN_FINALIZAR, callback_data=CB_FINALIZAR_PEDIR)]]
    )


def finalizar_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(BTN_FINALIZAR_SI, callback_data=CB_FINALIZAR_SI),
        InlineKeyboardButton(BTN_FINALIZAR_NO, callback_data=CB_FINALIZAR_NO),
    ]])


def build_application(settings: Settings, odoo: OdooClient, store: WatchStore) -> Application:
    for name, value in (("WEBAPP_BASE_URL", settings.webapp_base_url),
                        ("SCANNER_BASE_URL", settings.scanner_base_url),
                        ("PREOP_BASE_URL", settings.preop_base_url or settings.scanner_base_url)):
        if not value.startswith("https://"):
            raise ValueError(f"{name} debe ser una URL HTTPS pública (requisito de la Mini App)")
    if settings.telegram_mode == "webhook" and not settings.telegram_webhook_secret:
        raise ValueError("TELEGRAM_WEBHOOK_SECRET es obligatorio cuando TELEGRAM_MODE=webhook")

    builder = ApplicationBuilder().token(settings.telegram_bot_token.get_secret_value())
    if settings.telegram_mode == "webhook":
        builder = builder.updater(None)
    application = builder.build()

    async def notify(chat_id: int, ticket: dict) -> None:
        await application.bot.send_message(
            chat_id=chat_id, text=formatting.boarding_confirmed(ticket), parse_mode="HTML"
        )

    def fetch(ids: list[int]) -> dict[int, dict]:
        return find_tickets_by_ids(odoo, ids)

    access = AccessControl(
        lookup=lambda uid: find_employee_by_pin(odoo, uid),
        ttl_seconds=settings.employee_cache_seconds,
    )
    passenger = PassengerNotifier(
        settings.boarding_bot_internal_url,
        token_lookup=lambda: find_internal_token(odoo),
    )
    if not passenger.enabled:
        logger.warning("BOARDING_BOT_INTERNAL_URL vacío: no se avisará al pasajero del abordaje")

    async def notify_passenger(ticket_id: int, status: str) -> str:
        return await asyncio.to_thread(passenger.notify_boarding, ticket_id, status)

    watcher = Watcher(store, fetch, notify, settings.poll_interval_seconds,
                      notify_passenger=notify_passenger)
    application.bot_data[SERVICES_KEY] = Services(settings, odoo, access, store, watcher)

    # Va primero: mientras dura el registro, sus respuestas no deben llegar a on_text.
    application.add_handler(registro_conversation())
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("ayuda", cmd_start))
    application.add_handler(CommandHandler("formularios", cmd_formularios))
    application.add_handler(CommandHandler("preoperacional", cmd_preop))
    application.add_handler(CommandHandler("ast", cmd_ast))
    application.add_handler(CommandHandler("prueba_ruta", cmd_prueba_ruta))
    application.add_handler(CommandHandler("combustible", cmd_combustible))
    application.add_handler(CommandHandler("escanear", cmd_escanear))
    application.add_handler(CommandHandler("finalizar", cmd_finalizar))
    application.add_handler(CallbackQueryHandler(on_finalizar_confirm, pattern=r"^finalizar:(si|no|pedir)$"))
    application.add_handler(MessageHandler(filters.LOCATION & ~filters.UpdateType.EDITED, on_location))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return application


def _services(context: ContextTypes.DEFAULT_TYPE) -> Services:
    return context.application.bot_data[SERVICES_KEY]


async def _authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    assert update.effective_user is not None and update.message is not None  # siempre presentes: mensaje de chat
    user_id = str(update.effective_user.id)
    allowed = await asyncio.to_thread(_services(context).access.is_authorized, user_id)
    if allowed:
        return True
    registro = _registro(context)
    if registro is not None:
        # Usuario nuevo: en lugar de bloquearlo se le ofrece 📝 Registro (antes del preoperacional).
        if not await asyncio.to_thread(preop_registro.ya_registrado, registro.db, update.effective_user.id):
            await update.message.reply_text(MSG_REG_INVITACION, parse_mode="HTML", reply_markup=registro_keyboard())
        else:
            await update.message.reply_text(MSG_REG_SIN_ACCESO)
        return False
    await update.message.reply_text(
        "🔒 <b>Acceso no autorizado</b>.\n\n"
        f"Tu ID de Telegram es <code>{escape(user_id)}</code>. Pide a Recursos Humanos "
        "que lo registre en el campo <b>PIN</b> de tu empleado en Odoo.",
        parse_mode="HTML",
    )
    return False


def _registro(context: ContextTypes.DEFAULT_TYPE) -> RegistroServices | None:
    return context.application.bot_data.get(REGISTRO_KEY)


# ───────────────────────────────────── 📝 Registro ─────────────────────────────────────

def registro_conversation() -> ConversationHandler:
    """Conversación de registro: nombre -> documento -> correo -> validación en Odoo."""
    texto = filters.TEXT & ~filters.COMMAND
    return ConversationHandler(
        entry_points=[
            CommandHandler("registro", reg_inicio),
            MessageHandler(filters.Text([BTN_REGISTRO]), reg_inicio),
        ],
        states={
            REG_NOMBRE: [MessageHandler(texto, reg_nombre)],
            REG_DOCUMENTO: [MessageHandler(texto, reg_documento)],
            REG_CORREO: [MessageHandler(texto, reg_correo)],
        },
        fallbacks=[CommandHandler(["cancelar", "cancel"], reg_cancelar)],
        name="registro",
    )


async def reg_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """📝 Registro: solo para usuarios nuevos (sin acceso y sin registro completado)."""
    assert update.message is not None and update.effective_user is not None
    uid = update.effective_user.id
    if await asyncio.to_thread(_services(context).access.is_authorized, str(uid)):
        await update.message.reply_text(MSG_REG_YA_REGISTRADO, reply_markup=main_keyboard())
        return ConversationHandler.END
    registro = _registro(context)
    if registro is None:
        await update.message.reply_text(MSG_REG_NO_DISPONIBLE)
        return ConversationHandler.END
    if await asyncio.to_thread(preop_registro.ya_registrado, registro.db, uid):
        await update.message.reply_text(MSG_REG_SIN_ACCESO)
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text(MSG_REG_NOMBRE, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
    return REG_NOMBRE


async def reg_nombre(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None
    nombre = preop_registro.limpiar_nombre(update.message.text or "")
    if nombre is None:
        await update.message.reply_text(MSG_REG_ERROR_NOMBRE)
        return REG_NOMBRE
    context.user_data["reg_nombre"] = nombre
    await update.message.reply_text(MSG_REG_DOCUMENTO.format(nombre=escape(nombre.split(" ")[0])),
                                    parse_mode="HTML")
    return REG_DOCUMENTO


async def reg_documento(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None
    documento = preop_registro.limpiar_documento(update.message.text or "")
    if documento is None:
        await update.message.reply_text(MSG_REG_ERROR_DOCUMENTO)
        return REG_DOCUMENTO
    context.user_data["reg_documento"] = documento
    await update.message.reply_text(MSG_REG_CORREO, parse_mode="HTML")
    return REG_CORREO


async def reg_correo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None and update.effective_user is not None
    correo = preop_registro.limpiar_correo(update.message.text or "")
    if correo is None:
        await update.message.reply_text(MSG_REG_ERROR_CORREO)
        return REG_CORREO
    registro = _registro(context)
    if registro is None:
        await update.message.reply_text(MSG_REG_NO_DISPONIBLE)
        return ConversationHandler.END
    user = update.effective_user
    datos = preop_registro.DatosRegistro(
        telegram_user_id=user.id, telegram_username=user.username,
        nombre_completo=context.user_data.pop("reg_nombre"),
        documento=context.user_data.pop("reg_documento"), correo=correo,
    )
    await update.message.reply_text(MSG_REG_VALIDANDO)
    resultado = await asyncio.to_thread(
        preop_registro.registrar_conductor, registro.db, registro.client_factory, registro.company_ids, datos
    )
    if resultado.estado == preop_registro.REGISTRADO:
        _services(context).access.forget(str(user.id))  # el acceso se vuelve a consultar en Odoo
        await update.message.reply_text(MSG_REG_EXITO, reply_markup=main_keyboard())
    elif resultado.estado == preop_registro.NO_ENCONTRADO:
        await update.message.reply_text(MSG_REG_NO_ENCONTRADO, reply_markup=registro_keyboard())
    elif resultado.estado == preop_registro.PIN_OCUPADO:
        await update.message.reply_text(MSG_REG_PIN_OCUPADO, reply_markup=registro_keyboard())
    else:
        await update.message.reply_text(MSG_REG_ERROR_ODOO, parse_mode="HTML", reply_markup=registro_keyboard())
    return ConversationHandler.END


async def reg_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None
    context.user_data.pop("reg_nombre", None)
    context.user_data.pop("reg_documento", None)
    await update.message.reply_text(MSG_REG_CANCELADO, parse_mode="HTML", reply_markup=registro_keyboard())
    return ConversationHandler.END


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update, context):
        return
    assert update.message is not None
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML", reply_markup=main_keyboard())


async def cmd_formularios(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update, context):
        return
    assert update.message is not None
    await update.message.reply_text("📂 Formularios disponibles:",
                                    reply_markup=formularios_inline(_services(context).settings))


async def _open_option(update: Update, context: ContextTypes.DEFAULT_TYPE, option: MenuOption) -> None:
    if not await _authorized(update, context):
        return
    assert update.message is not None
    await update.message.reply_text(option.prompt, reply_markup=option_inline(option, _services(context).settings))


async def cmd_preop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _open_option(update, context, _FORM_BY_LABEL[BTN_PREOP])


async def cmd_ast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _open_option(update, context, _FORM_BY_LABEL[BTN_AST])


async def cmd_prueba_ruta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _open_option(update, context, _FORM_BY_LABEL[BTN_PRUEBA_RUTA])


async def cmd_combustible(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _open_option(update, context, _FORM_BY_LABEL[BTN_COMBUSTIBLE])


async def cmd_escanear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _open_option(update, context, ESCANER)


async def on_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Botón 📍: guarda la ubicación en la BD del bot y la asocia al preoperacional reciente."""
    if not await _authorized(update, context):
        return
    assert update.message is not None and update.message.location is not None
    assert update.effective_user is not None
    db = context.application.bot_data.get(PREOP_DB_KEY)
    if db is None:
        await update.message.reply_text("El registro de ubicación no está disponible en este momento.")
        return
    loc = update.message.location
    registro = await asyncio.to_thread(
        preop_service.registrar_ubicacion, db, update.effective_user.id,
        loc.latitude, loc.longitude, loc.horizontal_accuracy,
    )
    # Tanqueo pendiente de ubicación: el mensaje reenviado o anterior al registro no cuenta.
    asignada = None
    msg = update.message
    if getattr(msg, "forward_origin", None) is None and getattr(msg, "date", None) is not None:
        try:
            asignada = await asyncio.to_thread(
                combustible_service.asignar_ubicacion, db, update.effective_user.id,
                loc.latitude, loc.longitude, msg.date)
        except Exception:
            logger.exception("No se pudo asignar la ubicación al combustible pendiente")
    if asignada is None:
        await msg.reply_text(formatting.ubicacion_registrada(registro), parse_mode="HTML",
                             reply_markup=main_keyboard())
        return
    await msg.reply_text(
        formatting.combustible_ubicacion_registrada(asignada.placa, asignada.pendientes), parse_mode="HTML",
        reply_markup=ubicacion_keyboard() if asignada.pendientes else main_keyboard())


async def cmd_finalizar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """🔴 Finalizar: pregunta si terminó la jornada antes de liberar el vehículo reservado."""
    if not await _authorized(update, context):
        return
    assert update.message is not None
    await update.message.reply_text(FINALIZAR_PREGUNTA, reply_markup=finalizar_inline())


async def on_finalizar_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Respuesta a la confirmación.

    🔴 Finalizar (panel): muestra la pregunta en lugar del panel.
    ✅ Sí, finalizar: libera el vehículo del id de Telegram que pulsa el botón y avisa al jefe.
    ❌ Cancelar: no cambia nada y vuelve al panel de formularios.
    """
    query = update.callback_query
    assert query is not None and update.effective_user is not None
    if query.data == CB_FINALIZAR_PEDIR:
        # 🔴 Finalizar desde el panel de formularios: misma pregunta que el comando /finalizar.
        allowed = await asyncio.to_thread(_services(context).access.is_authorized, str(update.effective_user.id))
        if not allowed:
            await query.answer("🔒 Acceso no autorizado: tu ID de Telegram no está registrado.", show_alert=True)
            return
        await query.answer()
        await query.edit_message_text(FINALIZAR_PREGUNTA, reply_markup=finalizar_inline())
        return
    await query.answer()
    if query.data != CB_FINALIZAR_SI:
        await query.edit_message_text("📂 Formularios disponibles:",
                                      reply_markup=formularios_inline(_services(context).settings))
        return
    db = context.application.bot_data.get(PREOP_DB_KEY)
    if db is None:
        await query.edit_message_text("Finalizar no está disponible en este momento. Intenta de nuevo.")
        return
    liberacion = await asyncio.to_thread(preop_reservas.finalizar_jornada, db, update.effective_user.id)
    logger.info("Finalizar: usuario %s liberó %s", update.effective_user.id, ", ".join(liberacion.placas) or "nada")
    await query.edit_message_text(formatting.jornada_finalizada(liberacion.placas), parse_mode="HTML")
    if liberacion.placas and liberacion.jefe_telegram_id:
        try:
            await context.bot.send_message(chat_id=liberacion.jefe_telegram_id,
                                           text=formatting.jornada_finalizada_jefe(liberacion), parse_mode="HTML")
        except Exception:  # p. ej. el jefe nunca abrió el bot: Telegram no permite escribirle
            logger.exception("No se pudo avisar al jefe (Telegram %s) del fin de jornada", liberacion.jefe_telegram_id)
    elif liberacion.placas:
        logger.warning("Fin de jornada de %s: su jefe no tiene id de Telegram en el PIN", liberacion.conductor)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None  # filters.TEXT exige un mensaje de texto
    text = (update.message.text or "").strip()
    if text == BTN_FORMULARIOS:
        await cmd_formularios(update, context)
        return
    # Escanear QR y Finalizar ya no están en el teclado (van en el panel de formularios), pero
    # se siguen atendiendo para quienes aún tengan el teclado anterior.
    if text == BTN_ESCANEAR:
        await _open_option(update, context, ESCANER)
        return
    if text == BTN_FINALIZAR:
        await cmd_finalizar(update, context)
        return
    if text == BTN_UBICACION:
        # Clientes sin soporte de ubicación (p. ej. Telegram Desktop) envían el texto del botón.
        await update.message.reply_text("Para compartir tu ubicación abre el bot desde el celular "
                                        f"y toca <b>{BTN_UBICACION}</b>.", parse_mode="HTML",
                                        reply_markup=main_keyboard())
        return
    if ticket_state.extract_token(text) is None:
        await update.message.reply_text(
            "Elige una opción del menú. Para validar un pasajero pulsa "
            f"<b>{BTN_ESCANEAR}</b> y lee su QR.",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return
    if not await _authorized(update, context):
        return
    await _reply_scan(update, context, text)


async def _reply_scan(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
    """Respaldo por chat: pegar la URL del QR. Misma lógica que la Mini App."""
    assert update.effective_chat is not None and update.message is not None
    svc = _services(context)
    chat_id = update.effective_chat.id
    outcome = await asyncio.to_thread(
        scan_qr, svc.odoo, svc.store, svc.settings.watch_ttl_hours, payload, chat_id
    )
    if outcome.watching:
        svc.watcher.ensure_running()
    if outcome.status != "ok":
        await update.message.reply_text(STATUS_MESSAGES.get(outcome.status, "No fue posible leer el QR."))
        return
    assert outcome.ticket is not None  # status == "ok" garantiza un ticket presente (ver scan_qr)

    reason = ticket_state.describe(outcome.ticket, utc_now())
    parts = [formatting.ticket_card(outcome.ticket)]
    if reason:
        parts.append(escape(reason))
    if outcome.watching:
        parts.append("👀 Te avisaré aquí cuando se registre el abordaje.")
    url = f"{svc.settings.webapp_base_url.rstrip('/')}/form/accept/{outcome.token}"
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎫 Abrir página de abordaje", web_app=WebAppInfo(url=url))]]
    )
    await update.message.reply_text("\n\n".join(parts), parse_mode="HTML", reply_markup=keyboard)
