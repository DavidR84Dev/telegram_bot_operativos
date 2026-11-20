"""Handlers de @QRLectorBot: menú principal de Mini Apps y aviso de abordaje.

El bot es el punto de entrada de las funcionalidades operativas. Cada opción del
menú es una entrada de ``MENU`` (en el orden en que se muestra): agregar una
funcionalidad nueva = agregar una ``MenuOption`` y, si hace falta, su comando.

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
    Update,
    WebAppInfo,
)
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from . import formatting, ticket_state
from .access import AccessControl
from .config import Settings
from .odoo_client import OdooClient
from .odoo_queries import find_employee_by_pin, find_internal_token, find_tickets_by_ids
from .passenger_notifier import PassengerNotifier
from .scan_service import STATUS_MESSAGES, scan_qr
from .watch_store import WatchStore
from .watcher import Watcher, utc_now

logger = logging.getLogger(__name__)

SERVICES_KEY = "services"
BTN_ESCANEAR = "📷 Escanear QR"
BTN_PREOP = "📝 Preoperacional"


@dataclass(frozen=True)
class MenuOption:
    """Opción del menú principal: botón de teclado -> botón inline que abre la Mini App.

    Las Mini Apps se abren con botones *inline*: las lanzadas desde un botón de
    teclado llegan sin ``initData`` y no se pueden autenticar.
    """

    label: str                          # texto del botón del teclado (y del mensaje que envía)
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


BOT_COMMANDS = [
    BotCommand("start", "Abrir el menú principal"),
    BotCommand("preoperacional", "Diligenciar la inspección preoperacional"),
    BotCommand("escanear", "Abrir el escáner de QR"),
    BotCommand("ayuda", "Qué hace cada opción del menú"),
]


def scanner_url(base: str) -> str:
    return f"{base.rstrip('/')}/scanner"


def preop_url(settings: Settings) -> str:
    base = settings.preop_base_url or settings.scanner_base_url
    return f"{base.rstrip('/')}/preoperacional"


def scanner_inline(base: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📷 Abrir escáner", web_app=WebAppInfo(url=scanner_url(base)))]]
    )


# Orden del menú = orden de esta tupla (una opción por fila).
MENU: tuple[MenuOption, ...] = (
    MenuOption(
        label=BTN_PREOP,
        open_text="📝 Abrir preoperacional",
        prompt="Toca para abrir el formulario preoperacional:",
        help=f"<b>{BTN_PREOP}</b>: inspección preoperacional del vehículo antes de salir.",
        url=preop_url,
    ),
    MenuOption(
        label=BTN_ESCANEAR,
        open_text="📷 Abrir escáner",
        prompt="Toca para abrir el escáner:",
        help=(f"<b>{BTN_ESCANEAR}</b>: lee el QR del pasajero, muestra la ficha del ticket y el botón "
              "de la página de abordaje. Cuando se registre el abordaje recibirás aquí la confirmación."),
        url=lambda settings: scanner_url(settings.scanner_base_url),
    ),
)
_MENU_BY_LABEL = {option.label: option for option in MENU}

HELP_TEXT = "🚍 <b>Menú operativo</b>\n\n" + "\n\n".join(option.help for option in MENU)


def main_keyboard() -> ReplyKeyboardMarkup:
    """Teclado persistente del menú principal (botones de texto, uno por fila, en el orden de ``MENU``)."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton(option.label)] for option in MENU],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Elige una opción del menú",
    )


def option_inline(option: MenuOption, settings: Settings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(option.open_text, web_app=WebAppInfo(url=option.url(settings)))]])


def menu_inline(settings: Settings) -> InlineKeyboardMarkup:
    """Todas las Mini Apps como botones inline, en el orden del menú."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(o.open_text, web_app=WebAppInfo(url=o.url(settings)))] for o in MENU]
    )


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

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("ayuda", cmd_start))
    application.add_handler(CommandHandler("preoperacional", cmd_preop))
    application.add_handler(CommandHandler("escanear", cmd_escanear))
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
    await update.message.reply_text(
        "🔒 <b>Acceso no autorizado</b>.\n\n"
        f"Tu ID de Telegram es <code>{escape(user_id)}</code>. Pide a Recursos Humanos "
        "que lo registre en el campo <b>PIN</b> de tu empleado en Odoo.",
        parse_mode="HTML",
    )
    return False


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update, context):
        return
    assert update.message is not None
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML", reply_markup=main_keyboard())
    await update.message.reply_text("Toca una opción para abrirla:",
                                    reply_markup=menu_inline(_services(context).settings))


async def _open_option(update: Update, context: ContextTypes.DEFAULT_TYPE, option: MenuOption) -> None:
    if not await _authorized(update, context):
        return
    assert update.message is not None
    await update.message.reply_text(option.prompt, reply_markup=option_inline(option, _services(context).settings))


async def cmd_preop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _open_option(update, context, _MENU_BY_LABEL[BTN_PREOP])


async def cmd_escanear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _open_option(update, context, _MENU_BY_LABEL[BTN_ESCANEAR])


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None  # filters.TEXT exige un mensaje de texto
    text = update.message.text or ""
    option = _MENU_BY_LABEL.get(text.strip())
    if option is not None:
        await _open_option(update, context, option)
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
