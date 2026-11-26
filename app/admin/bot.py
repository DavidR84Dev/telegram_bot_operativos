"""Bot del Panel Admin: ``/start`` ofrece el botón Web App 🖥️ PORTAL (solo a los administradores)."""
from __future__ import annotations

import logging
from typing import Iterable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

logger = logging.getLogger(__name__)

PORTAL_LABEL = "🖥️ PORTAL"
RECHAZO = ("Este bot es solo para administradores. Si necesitas acceso, pídelo al área de TI "
           "indicando tu ID de Telegram: {user_id}.")
SIN_URL = "El panel todavía no está configurado (falta ADMIN_WEBAPP_BASE_URL). Avisa al área de TI."


def portal_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(PORTAL_LABEL, web_app=WebAppInfo(url=url))]])


def make_start(admin_ids: Iterable[int], webapp_url: str):
    permitidos = frozenset(admin_ids)

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or update.message is None:
            return
        user_id = update.effective_user.id
        if user_id not in permitidos:
            logger.warning("Panel Admin: /start rechazado para el usuario %s", user_id)
            await update.message.reply_text(RECHAZO.format(user_id=user_id))
            return
        if not webapp_url:
            await update.message.reply_text(SIN_URL)
            return
        await update.message.reply_text("Selecciona un formulario:", reply_markup=portal_keyboard(webapp_url))

    return start


def build_application(token: str, admin_ids: Iterable[int], webapp_url: str) -> Application:
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", make_start(admin_ids, webapp_url)))
    return application


async def configure_menu(application: Application, webapp_url: str) -> None:
    """Botón de menú junto al campo de mensaje (Web App). Un fallo no debe impedir el arranque."""
    if not webapp_url:
        return
    try:
        await application.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="PORTAL", web_app=WebAppInfo(url=webapp_url)))
        await application.bot.set_my_commands([("start", "Abrir el panel")])
    except Exception:
        logger.exception("No se pudo configurar el botón de menú del Panel Admin")
