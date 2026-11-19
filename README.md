# Telegram Bot Operativos — Validador de QR de abordaje

Servicio independiente (FastAPI + `python-telegram-bot`) que usa el validador de abordaje (conductor/auxiliar) para leer el QR del ticket del pasajero y confirmar el abordaje en Odoo.

- **Qué NO hace**: no escribe en Odoo. Toda consulta es de **solo lectura** (XML-RPC `search_read`). La confirmación del abordaje la hace la propia página web de Odoo (`programacion_operativos`), abierta desde la Mini App.
- **Quién lo usa**: personal autorizado (validadores) cuyo Telegram ID está en el campo `pin` de su `hr.employee` en Odoo, con `active = True`.
- **Stack**: FastAPI, `python-telegram-bot` 22.x, SQLAlchemy (SQLite en desarrollo/pruebas, PostgreSQL en Docker/producción), XML-RPC hacia Odoo.
- **Puerto**: `127.0.0.1:8011` (sin exponer directamente; en producción va detrás de nginx/TLS).
- **Empaquetado**: `Dockerfile` + `docker-compose.yml` (app + PostgreSQL). Ver [Despliegue con Docker](#puesta-en-marcha-docker-recomendado).

## Por qué existe este proyecto (visión)

Este servicio es el primero de varios **bots de Telegram operativos** pensados para centralizarse en este mismo repositorio/VPS, cada uno conectado en doble vía con Odoo mediante endpoints propios:

- **`telegram_bot_operativos`** (este proyecto): validación de QR de abordaje. Solo lectura hacia Odoo.
- **`telegram_boarding_bot`** (proyecto hermano, en `/opt/odoo/custom/telegram_boarding_bot`): emite el pase de abordaje al pasajero y expone una API interna (`/internal/notify-boarding`) que este validador usa para avisar al pasajero cuando se registra el abordaje.

Ambos corren hoy en el servidor de QA como procesos independientes (distinto puerto, distinto `.venv`, distinto `.env`) y están pensados para desplegarse como **servicios systemd separados** en un VPS de producción, siguiendo el mismo patrón (ver [Despliegue a producción](#despliegue-a-producción-vps)). Un futuro tercer bot debería copiar esta misma estructura: carpeta propia, `.venv` propio, `.env` propio, unidad systemd propia, subdominio propio.

## Arquitectura

```
Telegram ──(Mini App /scanner, showScanQrPopup)──► validador.<dominio> (VPS, nginx) ──► 127.0.0.1:8011 (contenedor "app")
                                                                                            │         │
Odoo (XML-RPC, solo lectura) ◄─────────────────────────────────────────────────────────────┘         │ estado de vigilancias
                                                                                            │         ▼
telegram_boarding_bot ◄──HTTPS POST /internal/notify-boarding (Bearer)────────────────────┘   contenedor "db" (PostgreSQL)
```

`app` y `db` corren como servicios de `docker-compose.yml` en la misma red interna de Docker; nginx (fuera de este compose, en el host) hace el proxy HTTPS hacia `127.0.0.1:8011`.

## Flujo de validación de abordaje

1. El validador pulsa **📷 Escanear QR** (botón de teclado) dentro del chat del bot.
2. Se abre la Mini App `SCANNER_BASE_URL/scanner`, que usa el escáner **nativo** de Telegram (`Telegram.WebApp.showScanQrPopup()`). No se procesan fotos ni hay OpenCV: la Mini App exige dispositivos móviles con Telegram actualizado (Bot API ≥ 6.4).
3. La página envía el texto leído a `POST /api/scan`, firmado con el `initData` de Telegram (cabecera `X-Telegram-Init-Data`).
4. El servidor:
   - Valida la firma HMAC-SHA256 de `initData` contra el token del bot y su antigüedad (`WEBAPP_AUTH_MAX_AGE_SECONDS`, por defecto 300 s) — ver `app/webapp_auth.py`.
   - Comprueba la whitelist: `hr.employee.pin == id de Telegram` y `active = True` (`app/access.py`, caché 300 s / negativos 60 s).
   - Extrae el `access_token` de la URL del QR (`.../form/accept/<token>`, se ignora el host — opción B) y consulta el ticket en `reservations.tickets` (solo lectura).
5. Si el ticket es "abordable" (`enviado`, o `abordado1` con 2.ª ruta pendiente) y tiene token vigente, se muestra la ficha y el botón **Abrir página de abordaje** (`WEBAPP_BASE_URL/form/accept/<token>`), que lleva a la propia página de Odoo donde se confirma el abordaje.
6. Se registra una **vigilancia** (SQLite, `app/watch_store.py`) del ticket. Un sondeo (`app/watcher.py`) revisa cada `POLL_INTERVAL_SECONDS` (15 s por defecto) los tickets vigilados **solo mientras haya alguno pendiente**:
   - Si el estado avanza (`enviado → abordado1 → abordado2`), avisa al validador en el chat de Telegram y, por separado, dispara el aviso al pasajero vía `telegram_boarding_bot` (`BOARDING_BOT_INTERNAL_URL` + token interno leído de `bot.telegram.config.internal_token`).
   - Si el ticket se cancela, se archiva, el token vence o la vigilancia caduca (`WATCH_TTL_HOURS`, 24 h por defecto), se elimina la vigilancia.
7. Respaldo sin Mini App: en el chat se puede pegar directamente la URL del QR como texto (misma lógica de `scan_qr`, sin lectura de fotos).

### Estados de un ticket (`app/ticket_state.py`)

| Estado | Significado | ¿Abordable? |
|---|---|---|
| `solicitud` | Sin vehículo asignado | No |
| `enviado` | Ticket válido, pendiente de 1.ª ruta | Sí |
| `abordado1` | 1.ª ruta confirmada | Sí, solo si tiene 2.ª ruta pendiente |
| `abordado2` | 2.ª ruta confirmada (estado final) | No |
| `cancelado` | Cancelado (estado final) | No |

## Estructura del código

```
app/
  main.py            Entrypoint FastAPI: lifespan (arranca/para el bot y el sondeo), /health, webhook de Telegram
  config.py          Settings (pydantic-settings), todo viene de .env
  telegram_app.py    Comandos /start y /ayuda, botón del escáner, respaldo por texto, construcción de la Application
  web.py             GET /scanner (Mini App) y POST /api/scan (API de la Mini App)
  webapp_auth.py     Validación de initData de Telegram (HMAC-SHA256)
  access.py          Whitelist con caché (hr.employee.pin)
  scan_service.py    Lógica común de escaneo (la usan el chat y la Mini App)
  ticket_state.py    Reglas puras de estado/abordabilidad de un ticket (sin E/S)
  watcher.py         Sondeo de vigilancias + decisión pura (decide()) de notificar/mantener/eliminar
  watch_store.py     Persistencia de vigilancias (SQLite en dev/pruebas, PostgreSQL en Docker/producción)
  odoo_client.py     Cliente XML-RPC de SOLO LECTURA (únicamente search_read)
  odoo_queries.py    Consultas concretas (hr.employee, reservations.tickets, bot.telegram.config)
  passenger_notifier.py  Cliente HTTP hacia telegram_boarding_bot (aviso al pasajero)
  formatting.py      Textos HTML para los mensajes de Telegram
  static/scanner.html   Página de la Mini App (escáner nativo + ficha del ticket)
tests/               66 pruebas (pytest), ver CONTRIBUTING.md
Dockerfile           Imagen de la app (también usada para correr los tests en CI/local)
docker-compose.yml   Servicios "app" (este bot) y "db" (PostgreSQL) para desarrollo/producción
pyproject.toml       Configuración de ruff, mypy, pytest y cobertura (gate de calidad)
requirements-dev.txt Dependencias de desarrollo (ruff, mypy) sobre requirements.txt
CHANGELOG.md         Historial de versiones (versionado semántico)
PLAN_IMPLEMENTACION.md   Decisiones de diseño, estado de la implementación y pendientes de producción
```

## Variables de entorno

Todo se configura por `.env` (no hay modelo de configuración en Odoo para este bot, a diferencia de `telegram_boarding_bot`; la única excepción es el **token interno** compartido, que se lee de `bot.telegram.config.internal_token`).

| Variable | Descripción |
|---|---|
| `APP_ENV` | `production` normalmente; `testing` lo fijan los tests (no arranca Telegram ni Odoo) |
| `LOG_LEVEL` | Nivel de log (`INFO` por defecto) |
| `ODOO_URL` / `ODOO_DB` / `ODOO_USERNAME` / `ODOO_PASSWORD` | Conexión XML-RPC a Odoo. `ODOO_PASSWORD` es una **API key** (Preferencias → API Keys), no la contraseña |
| `RPC_TIMEOUT_SECONDS` | Timeout de las llamadas XML-RPC (20 s por defecto) |
| `TELEGRAM_BOT_TOKEN` | Token de @BotFather |
| `TELEGRAM_MODE` | `polling` (pruebas/localhost) o `webhook` (producción) |
| `TELEGRAM_WEBHOOK_SECRET` | Solo modo `webhook`; `openssl rand -hex 32` |
| `BOT_PUBLIC_DOMAIN` | Solo modo `webhook`; dominio público donde se registra `/telegram/webhook` |
| `WEBAPP_BASE_URL` | URL HTTPS pública donde vive `/form/accept/<token>` (el frontend de Odoo) |
| `SCANNER_BASE_URL` | URL HTTPS pública que sirve `/scanner` (este mismo servicio) |
| `BOARDING_BOT_INTERNAL_URL` | URL interna de `telegram_boarding_bot` para avisar al pasajero. Vacío = aviso desactivado |
| `WEBAPP_AUTH_MAX_AGE_SECONDS` | Antigüedad máxima aceptada de `initData` (300 s por defecto) |
| `LISTEN_HOST` / `LISTEN_PORT` | `127.0.0.1` / `8011` |
| `POLL_INTERVAL_SECONDS` | Frecuencia del sondeo de vigilancias (15 s por defecto) |
| `WATCH_TTL_HOURS` | Caducidad de una vigilancia sin resolver (24 h por defecto) |
| `EMPLOYEE_CACHE_SECONDS` | Caché de la whitelist de empleados (300 s por defecto) |
| `DATABASE_URL` | Backend del estado de vigilancias: `sqlite:///run/watches.sqlite3` (desarrollo/manual) o `postgresql+psycopg2://user:pass@host:5432/db` (Docker/producción). Al correr con `docker-compose`, se sobreescribe automáticamente apuntando al contenedor `db` |
| `POSTGRES_PASSWORD` | Solo la usa `docker-compose` para el contenedor `db` |

Plantilla completa: `.env.example`.

## Puesta en marcha: Docker (recomendado)

Requiere Docker Engine y el plugin `docker compose` (`docker compose version`). Si el usuario que opera el servidor no pertenece al grupo `docker`, usa `sudo docker compose ...` o `sg docker -c "docker compose ..."`.

```bash
cd /opt/odoo/custom/telegram_bot_operativos
cp .env.example .env            # completa los valores (ver tabla de arriba); genera POSTGRES_PASSWORD con openssl rand -hex 16
docker compose build
docker compose up -d            # levanta "db" (PostgreSQL) y espera a que esté sana antes de arrancar "app"
docker compose ps               # ambos servicios en estado "healthy"
curl -s http://127.0.0.1:8011/health   # {"status":"ok"}
```

- `DATABASE_URL` del `.env` se **ignora** al correr con Docker: `docker-compose.yml` lo sobreescribe apuntando siempre al contenedor `db` (ver `environment:` del servicio `app`). El valor de `.env` solo aplica si se corre sin Docker (ver más abajo).
- El volumen `./run` persiste logs; el volumen nombrado `pgdata` persiste la base de datos de PostgreSQL entre reinicios/recreaciones del contenedor.
- Reiniciar tras cambiar `.env` o el código: `docker compose up -d --build`.
- Ver logs: `docker compose logs app -f` / `docker compose logs db -f`.
- Apagar: `docker compose down` (mantiene los volúmenes; usa `down -v` solo si quieres borrar también la base de datos).

### Verificado en este servidor de QA

El flujo completo se probó de punta a punta en este entorno: build de la imagen, `docker compose up -d` con healthchecks en verde, `/health` respondiendo `200 {"status":"ok"}`, las 66 pruebas pasando **dentro** del contenedor (`docker compose run --rm app python -m pytest -q`), y un alta/baja real de una vigilancia contra el PostgreSQL del contenedor `db` (`INSERT` → `count=1` → `DELETE` → `count=0`).

## Puesta en marcha sin Docker (alternativa)

Útil para depurar con el debugger local o si Docker no está disponible.

```bash
cd /opt/odoo/custom/telegram_bot_operativos
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # completa los valores; deja DATABASE_URL=sqlite:///run/watches.sqlite3
.venv/bin/python -m pytest -q
```

Arranque manual en QA (polling, sin dominio público; `WEBAPP_BASE_URL`/`SCANNER_BASE_URL` apuntan a un túnel `cloudflared`):

```bash
setsid nohup .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8011 \
  < /dev/null > run/validator.log 2>&1 &
# Túnel del escáner (la URL *.trycloudflare.com cambia en cada reinicio):
setsid nohup /opt/odoo/custom/telegram_boarding_bot/bin/cloudflared tunnel --no-autoupdate \
  --url http://127.0.0.1:8011 < /dev/null > run/cloudflared_scanner.log 2>&1 &
```

> No corras a la vez el proceso manual (`uvicorn` directo) y `docker compose up`: ambos intentan escuchar en `127.0.0.1:8011` y el segundo que arranque falla. Detén uno antes de levantar el otro.

Ver también `PLAN_IMPLEMENTACION.md` para el detalle del estado de la implementación y los riesgos abiertos.

## Pruebas y calidad

```bash
.venv/bin/pip install -r requirements-dev.txt   # agrega ruff y mypy sobre requirements.txt
.venv/bin/python -m pytest -q                   # 66 pruebas; aplica --cov=app --cov-fail-under=70 (ver pyproject.toml)
.venv/bin/ruff check app tests                  # lint (pyflakes + pycodestyle + bugbear)
.venv/bin/mypy app                              # tipado estático de todo el paquete app/
```

Las tres pasan limpias hoy (66 tests, ~78% de cobertura, 0 hallazgos de `ruff`/`mypy`) y están pensadas como **gate de calidad**: cualquier cambio debería mantenerlas en verde antes de mergear. El mismo comando de pytest corre igual dentro del contenedor: `docker compose run --rm app python -m pytest -q`.

No hay pruebas contra un Odoo real: `app/odoo_client.py` se sustituye por dobles en los tests, y `tests/test_odoo_client_readonly.py` comprueba específicamente que el cliente **no** expone `write`/`create`/`call`. Detalle de las convenciones de tipado (`assert` para invariantes que `mypy` no puede ver, `# type: ignore` puntual donde una librería externa tipa de forma imprecisa) en `CONTRIBUTING.md`.

## Despliegue a producción (VPS)

Hoy corre en el servidor de QA ya **dockerizado** (`docker compose up -d`, verificado de punta a punta: build, healthchecks, `/health`, tests y escritura real en PostgreSQL — ver sección de Docker arriba). Para producción, el despliegue de la aplicación cambia poco respecto a QA; lo que cambia es el dominio, el modo de Telegram y la capa de red:

1. **VPS**: mismo servidor que `telegram_boarding_bot` (o uno dedicado), Ubuntu 22.04/24.04, con Docker Engine y el plugin `docker compose` instalados (`sudo apt install docker.io docker-compose-v2`, y agregar el usuario de despliegue al grupo `docker`).
2. **DNS**: subdominio fijo propio, p. ej. `validador.viacotur.com` → IP del VPS (TTL bajo antes del cambio).
3. **Código**: copiar/clonar esta carpeta a `/opt/odoo/custom/telegram_bot_operativos` en el VPS. No hace falta crear un `.venv` en el VPS si se opera solo con Docker.
4. **`.env`**: `cp .env.example .env`, `chmod 600 .env`, completar valores (incluye `POSTGRES_PASSWORD`, generado con `openssl rand -hex 16`). Usar un **usuario técnico de Odoo dedicado** (no el admin usado en pruebas) con permisos de solo lectura sobre `hr.employee`, `reservations.tickets` y `bot.telegram.config`.
5. **Construir y levantar**: `docker compose build && docker compose up -d`. Verificar `docker compose ps` (ambos servicios `healthy`) y `curl -s http://127.0.0.1:8011/health`.
6. **nginx + TLS**: vhost propio para el subdominio, igual que `telegram_boarding_bot` (ver `telegram_boarding_bot/deploy/`), proxy a `127.0.0.1:8011` (sin cambios: el contenedor publica el mismo puerto de loopback que antes el proceso manual). Certificado Let's Encrypt solo para ese subdominio.
7. **Modo webhook**: en `.env`, `TELEGRAM_MODE=webhook`, `TELEGRAM_WEBHOOK_SECRET` fijo (`openssl rand -hex 32`), `BOT_PUBLIC_DOMAIN` = subdominio. `app/telegram_app.py` rechaza arrancar en modo `webhook` sin `TELEGRAM_WEBHOOK_SECRET` (falla rápido con un mensaje claro en vez de un error críptico).
8. **`WEBAPP_BASE_URL`/`SCANNER_BASE_URL`**: dominios HTTPS fijos (ya no túneles `cloudflared`).
9. **Arranque al reiniciar el VPS**: el `restart: unless-stopped` de `docker-compose.yml` ya hace que ambos contenedores vuelvan a levantarse si Docker los reinicia; que **Docker mismo** arranque al encender el VPS es responsabilidad del paquete (`systemctl enable docker`, ya activo por defecto en Ubuntu). Si se prefiere operar con `systemctl status/restart` en vez de `docker compose`, crear una unidad fina que delegue en compose (ver plantilla abajo).
10. **Firewall**: solo 22/80/443 abiertos al exterior; el puerto 8011 queda en `127.0.0.1` (igual que antes de Docker).
11. **Verificación**: `/health` local y público, `getWebhookInfo` del bot sin `last_error_message`, un escaneo real de un ticket de prueba y confirmación de que llega el aviso al validador y al pasajero.

### Unidad systemd opcional (envoltorio de `docker compose`)

No es obligatoria (Docker ya reinicia los contenedores solo), pero si se quiere administrar con `systemctl` igual que `telegram_boarding_bot`, crear `deploy/telegram-bot-operativos.service`:

```ini
[Unit]
Description=Telegram Bot Operativos - Validador QR (Docker Compose)
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/odoo/custom/telegram_bot_operativos
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
```

## Operación habitual

| Tarea | Comando (Docker) |
|---|---|
| Ver estado | `docker compose ps` |
| Ver logs | `docker compose logs app -f` |
| Reiniciar la app (sin tocar la base) | `docker compose restart app` |
| Reiniciar todo tras cambiar código/`.env` | `docker compose up -d --build` |
| Entrar a PostgreSQL | `docker compose exec db psql -U watcher -d watches` |

Antes de reiniciar el contenedor `app` en caliente: revisar que no haya vigilancias importantes en curso (`docker compose exec db psql -U watcher -d watches -c "SELECT COUNT(*) FROM watches"`). Al estar en PostgreSQL (volumen `pgdata`), las vigilancias sobreviven al reinicio; el sondeo simplemente se reanuda al arrancar `lifespan` (`watcher.ensure_running()`).

## Pendientes y riesgos conocidos

- **Usuario de Odoo**: en QA se usa un usuario admin; en producción debe ser un usuario técnico dedicado (grupo `hr.group_hr_user` o similar, sin permisos de escritura).
- **Cookies de Odoo en el webview**: la página de abordaje se abre por navegación (no en `iframe`, por ser origen distinto). Confirmar en un móvil real que el botón "Confirmar" funciona dentro de la Mini App.
- **Dispositivos**: requiere un cliente de Telegram con `showScanQrPopup` (Bot API ≥ 6.4). Clientes antiguos no pueden usar el escáner.
- **Túneles `cloudflared` en QA**: la URL `*.trycloudflare.com` cambia en cada reinicio del túnel; hay que actualizar `WEBAPP_BASE_URL`/`SCANNER_BASE_URL` y reiniciar el validador (`docker compose restart app`). No aplica una vez en producción con dominio fijo.
- **Cobertura de pruebas**: ~78%, con huecos deliberados en `app/odoo_client.py` (35%, plomería XML-RPC que requeriría un Odoo real o mockear `xmlrpc.client` a bajo nivel) y en el `lifespan` de `app/main.py` (56%, arranque/apagado del bot de Telegram). No son prioritarios porque son plomería delgada ya acotada por `tests/test_odoo_client_readonly.py` y por las pruebas de integración de `app/web.py`/`app/telegram_app.py`.
- **Prueba manual pendiente**: validar en el móvil real del validador el flujo completo (escáner nativo, ficha, botón de Odoo, recepción del aviso de abordaje).

## Versionado

Ver `CHANGELOG.md`. El proyecto sigue [versionado semántico](https://semver.org/lang/es/) (`MAJOR.MINOR.PATCH`), reflejado también en `version=` de `FastAPI(...)` en `app/main.py` y en tags de git (`vX.Y.Z`) una vez el código esté versionado en el repositorio.

Detalle completo en `PLAN_IMPLEMENTACION.md`.
