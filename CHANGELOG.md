# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/).
Versionado: [SemVer](https://semver.org/lang/es/) (`MAJOR.MINOR.PATCH`).

## [Sin publicar]

## [1.1.0] - 2026-11-19

### Agregado
- `Dockerfile` y `docker-compose.yml`: servicios `app` (este bot) y `db` (PostgreSQL 16), verificados de punta a punta en el servidor de QA (build, healthchecks, `/health`, 66 pruebas dentro del contenedor, escritura/lectura real contra PostgreSQL).
- Backend de `WatchStore` sobre PostgreSQL para Docker/producción, usando SQLAlchemy Core con el mismo SQL que SQLite (desarrollo/pruebas). Nueva variable `DATABASE_URL` (ver "Cambiado").
- Gate de calidad: `ruff` (lint) y `mypy` (tipado estático, limpio en los 15 módulos de `app/`), más cobertura de pruebas (`pytest-cov`, umbral `--cov-fail-under=70`, ~78% actual). Configurado en `pyproject.toml`; dependencias de desarrollo en `requirements-dev.txt`.
- Validación de arranque: `TELEGRAM_MODE=webhook` sin `TELEGRAM_WEBHOOK_SECRET` ahora falla rápido con un mensaje claro en vez de un `AttributeError` críptico.
- `CHANGELOG.md` y política de versionado semántico (ver `CONTRIBUTING.md`).

### Cambiado
- Renombrado el proyecto: `telegram_qr_validator` → `telegram_bot_operativos`.
- `Settings.db_path` → `Settings.database_url` (variable de entorno `DB_PATH` → `DATABASE_URL`). Por defecto sigue siendo SQLite (`sqlite:///run/watches.sqlite3`): no es un cambio disruptivo para quien ya corría el bot sin Docker, solo cambia el nombre de la variable.

### Documentación
- `README.md` y `CONTRIBUTING.md` reescritos: arquitectura, flujo completo, variables de entorno, puesta en marcha con Docker (recomendada) y sin Docker, despliegue a producción en VPS, operación habitual, convenciones de tipado y calidad.

## [1.0.0] - 2026-11-19

Primera versión documentada (bajo el nombre `telegram_qr_validator`): validación de QR de abordaje vía Mini App de Telegram, consulta de solo lectura a Odoo, vigilancia de tickets con aviso al validador y al pasajero (vía `telegram_boarding_bot`), estado persistido en SQLite. 66 pruebas.
