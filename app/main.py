"""Entrypoint FastAPI: validador de QR (solo lectura en Odoo), preoperacional, AST y Prueba de Ruta."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from telegram import Update

from . import formatting
from .ast_trabajo.router import AstServices
from .ast_trabajo.router import router as ast_router
from .ast_trabajo.service import AstOutcome
from .ast_trabajo.worker import AstSender
from .config import get_settings
from .odoo_client import OdooClient
from .preop.db import Database
from .preop.etl import run_from_settings
from .preop.rest_client import OdooRestClient
from .preop.router import PreopServices
from .preop.router import router as preop_router
from .preop.service import SendOutcome
from .preop.worker import MasterSync, PreopSender, build_writer
from .prueba_ruta.acuerdos import acuerdos_sin_sincronizar, normalizar_lista
from .prueba_ruta.router import PruebaRutaServices
from .prueba_ruta.router import router as prueba_ruta_router
from .prueba_ruta.service import PruebaRutaOutcome
from .prueba_ruta.worker import PruebaRutaSender
from .telegram_app import (
    BOT_COMMANDS,
    PREOP_DB_KEY,
    REGISTRO_KEY,
    SERVICES_KEY,
    RegistroServices,
    build_application,
)
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

    writer = build_writer(settings)
    sender = PreopSender(
        db, writer, settings.preop_worker_interval_seconds, settings.preop_max_attempts,
        settings.preop_sending_lease_seconds, notify=notify,
    )
    sender.start()
    tasks: list = [sender]
    tasks.append(_start_ast(app, application, db, writer))
    tasks.append(_start_prueba_ruta(app, application, db, writer))
    # El handler de 📍 Enviar ubicación guarda en esta misma BD.
    application.bot_data[PREOP_DB_KEY] = db
    app.state.preop = PreopServices(
        db=db, bot_token=settings.telegram_bot_token.get_secret_value(),
        max_age_seconds=settings.preop_auth_max_age_seconds, wake_worker=sender.wake,
        odometro=writer.ultimo_odometro,
    )
    if settings.master_sync_enabled:
        master = MasterSync(settings.master_sync_interval_seconds, lambda: run_from_settings(db))
        master.start()
        tasks.append(master)
        # 📝 Registro de conductores nuevos: misma BD y misma API REST (/api/empleados).
        assert settings.odoo_api_password is not None
        api_password = settings.odoo_api_password.get_secret_value()
        application.bot_data[REGISTRO_KEY] = RegistroServices(
            db=db,
            client_factory=lambda: OdooRestClient(settings.odoo_api_url or settings.odoo_url,
                                                  settings.odoo_api_login, api_password,
                                                  settings.odoo_api_timeout_seconds),
            company_ids=settings.api_company_ids,
        )
    else:
        logger.warning("ODOO_API_LOGIN/ODOO_API_PASSWORD vacíos: tablas maestras sin sincronizar "
                       "y 📝 Registro desactivado")
    return tasks


def _start_ast(app: FastAPI, application, db: Database, writer) -> AstSender:
    """Arranca el AST: misma BD, mismo usuario de escritura y su propia cola hacia el modelo ``ast``."""

    async def notify(outcome: AstOutcome) -> None:
        await application.bot.send_message(
            chat_id=outcome.telegram_user_id, text=formatting.ast_result(outcome), parse_mode="HTML"
        )
        if outcome.aviso_jefe is not None:
            try:
                await application.bot.send_message(
                    chat_id=outcome.aviso_jefe.telegram_id,
                    text=formatting.ast_aviso_jefe(outcome.aviso_jefe, outcome.odoo_name), parse_mode="HTML",
                )
            except Exception:  # p. ej. el jefe nunca abrió el bot: Telegram no permite escribirle
                logger.exception("No se pudo avisar al jefe del AST (Telegram %s)", outcome.aviso_jefe.telegram_id)

    sender = AstSender(
        db, writer, settings.preop_worker_interval_seconds, settings.preop_max_attempts,
        settings.preop_sending_lease_seconds, notify=notify,
    )
    sender.start()
    app.state.ast = AstServices(
        db=db, bot_token=settings.telegram_bot_token.get_secret_value(),
        max_age_seconds=settings.preop_auth_max_age_seconds, wake_worker=sender.wake,
    )
    return sender


def _start_prueba_ruta(app: FastAPI, application, db: Database, writer) -> PruebaRutaSender:
    """Arranca la Prueba de Ruta: misma BD y usuario de escritura; cola propia hacia ``prueba.ruta``."""

    async def notify(outcome: PruebaRutaOutcome) -> None:
        await application.bot.send_message(
            chat_id=outcome.telegram_user_id, text=formatting.prueba_ruta_result(outcome), parse_mode="HTML"
        )
        if outcome.aviso_jefe is not None:
            try:
                await application.bot.send_message(
                    chat_id=outcome.aviso_jefe.telegram_id,
                    text=formatting.prueba_ruta_aviso_jefe(outcome.aviso_jefe, outcome.odoo_name), parse_mode="HTML",
                )
            except Exception:  # p. ej. el jefe nunca abrió el bot: Telegram no permite escribirle
                logger.exception("No se pudo avisar al jefe de la prueba de ruta (Telegram %s)",
                                 outcome.aviso_jefe.telegram_id)

    autorizados = normalizar_lista(settings.prueba_ruta_acuerdos_lista)
    if not autorizados:
        logger.warning("PRUEBA_RUTA_ACUERDOS vacío: ningún conductor podrá hacer la Prueba de Ruta")
    with db.session() as session:
        faltan = acuerdos_sin_sincronizar(session, autorizados)
    if faltan:
        logger.warning("PRUEBA_RUTA_ACUERDOS: sin acuerdo sincronizado con ese nombre (aún): %s", "; ".join(faltan))
    sender = PruebaRutaSender(
        db, writer, settings.preop_worker_interval_seconds, settings.preop_max_attempts,
        settings.preop_sending_lease_seconds, notify=notify,
    )
    sender.start()
    app.state.prueba_ruta = PruebaRutaServices(
        db=db, bot_token=settings.telegram_bot_token.get_secret_value(),
        max_age_seconds=settings.preop_auth_max_age_seconds, autorizados=autorizados, wake_worker=sender.wake,
    )
    return sender


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


app = FastAPI(title="Telegram Bot Operativos", version="1.4.0", lifespan=lifespan)
app.include_router(web_router)
app.include_router(preop_router)
app.include_router(ast_router)
app.include_router(prueba_ruta_router)


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
