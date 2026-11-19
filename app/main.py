"""Entrypoint FastAPI del validador de QR (solo consulta a Odoo)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from telegram import Update

from .config import get_settings
from .odoo_client import OdooClient
from .telegram_app import BOT_COMMANDS, SERVICES_KEY, build_application
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    application = None
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
    yield
    if application is not None:
        application.bot_data[SERVICES_KEY].watcher.stop()
        if settings.telegram_mode != "webhook":
            assert application.updater is not None
            await application.updater.stop()
        await application.stop()
        await application.shutdown()


app = FastAPI(title="Telegram Bot Operativos - Validador QR", version="1.1.0", lifespan=lifespan)
app.include_router(web_router)


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
