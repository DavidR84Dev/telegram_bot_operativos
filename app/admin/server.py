"""Aplicación FastAPI del Panel Admin (la lanza ``python -m app.admin``)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from telegram import Update

from ..config import get_settings
from ..preop.db import Database
from ..preop.router import _LOGO_URL, logo
from . import bot as admin_bot
from .router import AdminServices
from .router import router as admin_router

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
        if settings.admin_bot_token is None:
            raise RuntimeError("ADMIN_BOT_TOKEN vacío: el Panel Admin necesita su propio token de BotFather")
        token = settings.admin_bot_token.get_secret_value()
        if not settings.admin_ids:
            logger.warning("ADMIN_TELEGRAM_IDS vacío: nadie podrá entrar al panel")
        app.state.admin = AdminServices(
            db=Database(settings.database_url), bot_token=token, admin_ids=settings.admin_ids,
            env_file=Path(settings.admin_env_file), max_age_seconds=settings.admin_auth_max_age_seconds,
        )
        application = admin_bot.build_application(token, settings.admin_ids, settings.admin_webapp_base_url)
        await application.initialize()
        await application.start()
        assert application.updater is not None
        await application.bot.delete_webhook()
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        await admin_bot.configure_menu(application, settings.admin_webapp_base_url)
        logger.info("Panel Admin: polling activo")
    yield
    if application is not None:
        assert application.updater is not None
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


app = FastAPI(title="Panel Admin", version="1.0.0", lifespan=lifespan)
app.include_router(admin_router)
app.add_api_route(_LOGO_URL, logo, methods=["GET"], include_in_schema=False)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
