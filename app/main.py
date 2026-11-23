"""Entrypoint FastAPI: validador de QR (solo lectura en Odoo) y preoperacional (Mini App + cola a Odoo)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from telegram import Update

from . import formatting
from .config import get_settings
from .odoo_client import OdooClient
from .preop.db import Database
from .preop.etl import run_from_settings
from .preop.router import PreopServices
from .preop.router import router as preop_router
from .preop.service import SendOutcome
from .preop.worker import MasterSync, PreopSender, build_writer
from .telegram_app import BOT_COMMANDS, PREOP_DB_KEY, SERVICES_KEY, build_application
from .watch_store import WatchStore
from .web import router as web_router

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
# httpx registra la URL de cada petición a Telegram, que incluye el token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def _start_preop(app: FastAPI, application) -> list:
    """Arranca el módulo preoperacional: BD, cola de envío a Odoo y sincronización maestra."""
    db = Database(settings.database_url)

    async def notify(outcome: SendOutcome) -> None:
        await application.bot.send_message(
            chat_id=outcome.telegram_user_id, text=formatting.preop_result(outcome), parse_mode="HTML"
        )
        if outcome.aviso_jefe is not None:
            try:
                await application.bot.send_message(
                    chat_id=outcome.aviso_jefe.telegram_id,
                    text=formatting.preop_aviso_jefe(outcome.aviso_jefe, outcome.odoo_name), parse_mode="HTML",
                )
            except Exception:  # p. ej. el jefe nunca abrió el bot: Telegram no permite escribirle
                logger.exception("No se pudo avisar al jefe (Telegram %s)", outcome.aviso_jefe.telegram_id)

    sender = PreopSender(
        db, build_writer(settings), settings.preop_worker_interval_seconds, settings.preop_max_attempts,
        settings.preop_sending_lease_seconds, notify=notify,
    )
    sender.start()
    tasks: list = [sender]
    # El handler de 📍 Enviar ubicación guarda en esta misma BD.
    application.bot_data[PREOP_DB_KEY] = db
    app.state.preop = PreopServices(
        db=db, bot_token=settings.telegram_bot_token.get_secret_value(),
        max_age_seconds=settings.preop_auth_max_age_seconds, wake_worker=sender.wake,
    )
    if settings.master_sync_enabled:
        master = MasterSync(settings.master_sync_interval_seconds, lambda: run_from_settings(db))
        master.start()
        tasks.append(master)
    else:
        logger.warning("ODOO_API_LOGIN/ODOO_API_PASSWORD vacíos: tablas maestras sin sincronizar")
    return tasks


@asynccontextmanager
async def lifespan(app: FastAPI):
    application = None
    preop_tasks: list = []
    if settings.app_env != "testing":
        store = WatchStore(settings.database_url)
        application = build_application(settings, OdooClient(), store)
        await application.initialize()
        await application.start()
        if settings.telegram_mode == "webhook":
            # build_application() ya exigió TELEGRAM_WEBHOOK_SECRET para este modo.
            assert settings.telegram_webhook_secret is not None
            url = f"https://{settings.bot_public_domain}/telegram/webhook"
            await application.bot.set_webhook(
                url=url,
                secret_token=settings.telegram_webhook_secret.get_secret_value(),
                allowed_updates=Update.ALL_TYPES,
            )
            logger.info("Webhook registrado: %s", url)
        else:
            # Polling (localhost): un webhook previo impediría recibir updates.
            # Solo en modo webhook se construye la Application con updater(None).
            assert application.updater is not None
            await application.bot.delete_webhook()
            await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            logger.info("Modo polling activo")
        await application.bot.set_my_commands(BOT_COMMANDS)
        app.state.application = application
        app.state.services = application.bot_data[SERVICES_KEY]
        # Reanuda la vigilancia de tickets pendientes tras un reinicio.
        application.bot_data[SERVICES_KEY].watcher.ensure_running()
        preop_tasks = _start_preop(app, application)
    yield
    for task in preop_tasks:
        await task.stop()
    if application is not None:
        application.bot_data[SERVICES_KEY].watcher.stop()
        if settings.telegram_mode != "webhook":
            assert application.updater is not None
            await application.updater.stop()
        await application.stop()
        await application.shutdown()


app = FastAPI(title="Telegram Bot Operativos", version="1.2.0", lifespan=lifespan)
app.include_router(web_router)
app.include_router(preop_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    secret: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
) -> dict:
    expected = settings.telegram_webhook_secret.get_secret_value() if settings.telegram_webhook_secret else None
    if not expected or secret != expected:
        logger.warning("Webhook rechazado: secret inválido")
        raise HTTPException(status_code=403, detail="Invalid secret token")
    try:
        data = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    application = request.app.state.application
    update = Update.de_json(data, application.bot)
    await application.update_queue.put(update)
    return {"ok": True}
