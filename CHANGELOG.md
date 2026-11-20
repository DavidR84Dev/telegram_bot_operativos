# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/).
Versionado: [SemVer](https://semver.org/lang/es/) (`MAJOR.MINOR.PATCH`).

## [Sin publicar]

### Agregado
- **Preoperacional como bot de referencia (`@viacoturform_bot`)**:
  - Menú de @QRLectorBot: **📋 Formularios** (lista inline, ✅ Preoperacional primero), **📷 Escanear QR** y **📍 Enviar ubicación**; comando `/formularios`.
  - Mini App por pasos (Información → Verificación → Evidencia → Resumen) con barra de progreso e identidad VIACOTUR (logo y paleta).
  - Vehículo tomado y bloqueado desde el **extracto aprobado** del conductor; sin extracto aprobado, pantalla de bloqueo con instrucciones.
  - Foto de testigos del tablero obligatoria (comprimida en el teléfono), guardada en `preop_fotos` y enviada a `testigos_img` de Odoo.
  - Tabla `ubicaciones` en la BD del bot: ubicación compartida desde el chat, asociada al preoperacional de los últimos 10 minutos.
- **Preoperacional vehicular** (`app/preop/`, documentación en `docs/PREOPERACIONAL.md`):
  - Mini App `/preoperacional` (botón 📝 y comando `/preoperacional`) con autocompletado de vehículo, extracto y contrato desde tablas locales.
  - Tablas `empleados`, `flota`, `extractos` sincronizadas desde la API REST de Odoo (`viacotur_api`, JWT) con UPSERT por `odoo_id`, desactivación de los que desaparecen y bitácora `sync_runs`.
  - Tabla `preoperacional` (JSONB para checklist/llantas/atributos dinámicos) con auditoría de sincronización (`pendiente/enviando/enviado/error/fallido`, `odoo_id`, `error_log`, reintentos con backoff).
  - Worker asyncio que crea el registro en Odoo por XML-RPC sin bloquear al usuario; idempotente (external id + llave natural) y con aviso al conductor del resultado.
  - Validación HMAC-SHA256 de `initData` y payload estricto con Pydantic (`extra="forbid"`, catálogo de ítems verificado contra los campos de Odoo).
  - `scripts/introspect_endpoint.py`: genera modelos SQLAlchemy/Pydantic a partir de un endpoint.
  - CLI de operación: `python -m app.preop.etl`, `python -m app.preop.worker --once | --requeue ID… | --requeue-all`.
  - Token JWT de la API renovado antes de su vencimiento de 24 h (claim `exp`), con respaldo ante 401.
  - 30 pruebas nuevas (96 en total); verificadas también contra PostgreSQL 16 real (`PREOP_TEST_DATABASE_URL`).
- Dependencia `tenacity`.

### Cambiado
- Menú principal de @QRLectorBot como registro ordenado (`MENU`): **📝 Preoperacional** primero y **📷 Escanear QR** después, una opción por fila; `/start` muestra los botones inline en el mismo orden. Nuevo comando `/escanear`.
- `webapp_auth.validate_init_data_user()` devuelve el usuario completo de Telegram (`validate_init_data()` se mantiene igual).
- El servicio deja de ser estrictamente de solo lectura en Odoo: la única escritura es `create` de `preoperacional` (`OdooWriter`); `OdooClient` sigue siendo solo lectura.

### Corregido
- `docker-compose.yml`: `extra_hosts: host.docker.internal:host-gateway` para que el contenedor alcance Odoo y `telegram_boarding_bot` en el host sin depender de la IP de la LAN (que cambió por DHCP y dejó el validador sin Odoo).

### Documentación
- README: nueva sección "Acceso del contenedor a servicios del host" (host-gateway, regla de ufw para redes Docker, escucha del bot de abordaje y comprobación). Corregido: tras cambiar el `.env` hay que usar `docker compose up -d`, no `restart`.
- `.env.example`: valores recomendados de `ODOO_URL` y `BOARDING_BOT_INTERNAL_URL` con y sin Docker; eliminada la IP interna de QA.
- Pruebas: IP interna de QA sustituida por una de documentación (`192.0.2.10`, RFC 5737).

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
