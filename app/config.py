from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración del validador leída solo desde variables de entorno (o `.env`).

    Todo vive en `.env`: este bot no lee configuración de Odoo.
    En pruebas se fija ``APP_ENV=testing`` para no arrancar Telegram ni Odoo.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "production"
    log_level: str = "INFO"

    # Odoo (XML-RPC, solo lectura)
    odoo_url: str
    odoo_db: str
    odoo_username: str
    odoo_password: SecretStr
    rpc_timeout_seconds: int = 20

    # Telegram
    telegram_bot_token: SecretStr
    # polling: pruebas en localhost (sin dominio público). webhook: producción.
    telegram_mode: str = "polling"
    telegram_webhook_secret: SecretStr | None = None
    bot_public_domain: str = ""

    # URL HTTPS pública que abre la Mini App (túnel en qa2, dominio en producción).
    # Se usa en lugar del host del QR (opción B): solo se conserva el path /form/accept/<token>.
    webapp_base_url: str
    # HTTPS público que sirve la página /scanner de este validador (túnel en pruebas).
    scanner_base_url: str
    # URL interna del bot de pasajeros (mismo VPS) que envía el aviso de abordaje.
    # Vacía = aviso al pasajero desactivado. El token se lee de bot.telegram.config.
    boarding_bot_internal_url: str = ""
    # Antigüedad máxima aceptada de initData de Telegram (segundos).
    webapp_auth_max_age_seconds: int = 300

    # Servidor
    listen_host: str = "127.0.0.1"
    listen_port: int = 8011

    # Sondeo y estado
    poll_interval_seconds: int = 15
    watch_ttl_hours: int = 24
    employee_cache_seconds: int = 300
    # SQLite en desarrollo/pruebas (p. ej. "sqlite:///run/watches.sqlite3" o ":memory:");
    # PostgreSQL en Docker/producción (p. ej. "postgresql+psycopg2://user:pass@db:5432/watches").
    database_url: str = "sqlite:///run/watches.sqlite3"


@lru_cache
def get_settings() -> Settings:
    return Settings()
