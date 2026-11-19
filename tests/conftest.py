import os

# Antes de importar la app: configuración de prueba (sin Telegram ni Odoo reales).
os.environ.update({
    "APP_ENV": "testing",
    "ODOO_URL": "http://127.0.0.1:1",
    "ODOO_DB": "test",
    "ODOO_USERNAME": "test",
    "ODOO_PASSWORD": "test",
    "TELEGRAM_BOT_TOKEN": "123456:TEST",
    "TELEGRAM_WEBHOOK_SECRET": "s3cret",
    "WEBAPP_BASE_URL": "https://example.trycloudflare.com/",
    "SCANNER_BASE_URL": "https://scan.example.trycloudflare.com/",
    "DATABASE_URL": "sqlite:///:memory:",
})
