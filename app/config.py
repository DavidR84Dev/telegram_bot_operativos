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

    # ── Preoperacional (Mini App /preoperacional) ───────────────────────────
    # API REST de Odoo (módulo viacotur_api, JWT validador "e2et") para las tablas maestras.
    # Vacío = se usa ODOO_URL. Sin ODOO_API_LOGIN la sincronización maestra queda desactivada.
    odoo_api_url: str = ""
    odoo_api_login: str = ""
    # Contraseña real del usuario (no API key: /api/authenticate es un login interactivo).
    odoo_api_password: SecretStr | None = None
    # Compañías a sincronizar (ids de res.company separados por coma).
    odoo_api_company_ids: str = "1"
    odoo_api_page_size: int = 500
    odoo_api_timeout_seconds: int = 60
    master_sync_interval_seconds: int = 900

    # GPS de la flota (cotejo de la ubicación de «Combustible para gastos»). URL vacía = desactivado
    # (los gastos van con «GPS no disponible»). La clave va en la cabecera x-api-key.
    gps_flota_url: str = ""
    gps_flota_api_key: SecretStr | None = None
    gps_flota_timeout_seconds: float = 10

    # product_id (product.product) del gasto de combustible en hr.expense; cambia por ambiente
    # (producción 9629, qa2 77 «[I011] COMBUSTIBLE»).
    combustible_gastos_product_id: int = 9629

    # Escritura XML-RPC del preoperacional. Vacíos = mismas credenciales de ODOO_USERNAME/ODOO_PASSWORD.
    odoo_write_username: str = ""
    odoo_write_password: SecretStr | None = None
    # Cola de envío a Odoo.
    preop_worker_interval_seconds: int = 10
    preop_max_attempts: int = 8
    # Un registro "enviando" más viejo que esto se considera abandonado (caída del proceso) y se reintenta.
    preop_sending_lease_seconds: int = 300
    # HTTPS público de la Mini App del preoperacional. Vacío = SCANNER_BASE_URL (mismo servicio).
    preop_base_url: str = ""
    # Antigüedad máxima de initData en la Mini App del preoperacional: el formulario es largo
    # (se diligencia en varios minutos), por eso es mayor que la del escáner.
    preop_auth_max_age_seconds: int = 3600
    # Acuerdos (agreement) cuyos conductores pueden hacer la Prueba de Ruta, separados por ";".
    # Se comparan normalizados (sin mayúsculas, tildes, puntuación ni prefijo "[código]").
    # Agregar un contrato = sumarlo aquí y reiniciar.
    prueba_ruta_acuerdos: str = "MINERA DE COBRE QUEBRADONA;SERVICIOS HIDROGEOLÓGICOS INTEGRALES S.A.S."

    @property
    def api_company_ids(self) -> list[int]:
        return [int(part) for part in self.odoo_api_company_ids.split(",") if part.strip()]

    @property
    def prueba_ruta_acuerdos_lista(self) -> list[str]:
        return [part.strip() for part in self.prueba_ruta_acuerdos.split(";") if part.strip()]

    @property
    def master_sync_enabled(self) -> bool:
        return bool(self.odoo_api_login and self.odoo_api_password)


@lru_cache
def get_settings() -> Settings:
    return Settings()
