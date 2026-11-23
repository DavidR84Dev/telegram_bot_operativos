# Contribuir a `telegram_bot_operativos`

Guía rápida para trabajar en este proyecto. Para la descripción funcional y el despliegue, ver `README.md` y `PLAN_IMPLEMENTACION.md`.

Para levantar el proyecto completo (app + PostgreSQL) usa Docker (`README.md` → "Puesta en marcha: Docker"). Esta sección cubre el entorno **sin** Docker, útil para editar código con autocompletado/debugger local y para correr `ruff`/`mypy` desde el editor.

## Entorno de desarrollo

```bash
cd /opt/odoo/custom/telegram_bot_operativos
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # valores de prueba; ver tabla de variables en README.md
```

El `.venv` **no es portable**: sus scripts (`uvicorn`, `pytest`, etc.) llevan grabada la ruta absoluta de creación en el shebang. Si se mueve o renombra la carpeta del proyecto, hay que recrear el `.venv` (no basta con copiarlo):

```bash
rm -rf .venv && python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## Ejecutar las pruebas y el gate de calidad

```bash
.venv/bin/pip install -r requirements-dev.txt   # ruff + mypy, sobre requirements.txt
.venv/bin/python -m pytest -q                   # 66 pruebas + cobertura (--cov-fail-under=70, ver pyproject.toml)
.venv/bin/ruff check app tests                  # lint
.venv/bin/mypy app                               # tipado estático
```

Las tres deben pasar limpias antes de abrir un cambio. `tests/conftest.py` fija `APP_ENV=testing` y variables de entorno ficticias antes de importar la app, y `DATABASE_URL=sqlite:///:memory:` evita escribir en disco: ninguna prueba toca Telegram, Odoo ni PostgreSQL reales.

El mismo comando de pytest corre igual dentro de la imagen Docker (útil para verificar que "pasa en CI" == "pasa en lo que se despliega"):

```bash
docker compose build app && docker compose run --rm app python -m pytest -q
```

Antes de abrir un cambio, además:
- Si tocaste `app/odoo_client.py`, revisa que `tests/test_odoo_client_readonly.py` siga pasando — ese archivo existe específicamente para impedir que se añada `write`/`create`/`call` al cliente.
- Si tocaste reglas de estado (`app/ticket_state.py`) o la decisión del sondeo (`app/watcher.py::decide`), añade casos en `tests/test_ticket_state.py` / `tests/test_watcher.py`: son funciones puras (sin E/S) pensadas para probarse por tabla de casos, no hace falta mockear nada.
- Si agregaste código nuevo, revisa que la cobertura total no baje de 70% (`--cov-fail-under=70` hace fallar `pytest` si baja). Si el código nuevo es plomería difícil de probar sin un Odoo/Telegram real (como `app/odoo_client.py`), está bien que quede sin cubrir, pero dilo explícitamente en el PR en vez de forzar una prueba artificial.

### Convenciones de tipado (`mypy`)

`mypy app` corre limpio sobre las 15 fuentes de `app/`. Dos patrones explican casi todos los `assert`/`# type: ignore` que vas a encontrar:

- **`assert` para invariantes que el tipo no refleja.** Por ejemplo, `watcher.decide()` solo devuelve `"notify"` cuando el ticket no es `None`, pero `mypy` no puede ver esa relación entre dos funciones. En vez de debilitar los tipos (`dict | None` en todos lados) o duplicar el `if ticket is None`, se usa `assert ticket is not None` justo donde el invariante ya está garantizado por la rama en la que estás, con un comentario diciendo *por qué* es seguro. Mismo patrón en `app/telegram_app.py` para `update.message`/`update.effective_user` (garantizados por el tipo de handler registrado) y en `app/main.py` para `application.updater` (solo es `None` en modo webhook) y `settings.telegram_webhook_secret` (validado al construir la `Application`, ver `build_application()`).
- **`# type: ignore[código]` puntual cuando la librería externa tipa de forma imprecisa.** Usado solo en `app/odoo_client.py`: `xmlrpc.client` tipa `make_connection`/`execute_kw` de forma más laxa que como se usan en la práctica. Siempre con el código del error entre corchetes y un comentario de una línea explicando por qué es seguro ignorarlo — nunca un `# type: ignore` desnudo.

No agregues `# type: ignore` para silenciar un error que no entiendes: primero confirma si es un caso real (bug) o un invariante que un `assert` puede documentar.

## Convenciones del código

- **Escritura en Odoo acotada.** `app/odoo_client.py` solo expone `search_read` y toda consulta nueva va en `app/odoo_queries.py` como función de solo lectura. La **única** escritura del servicio es `OdooWriter.create_preoperacional` (`app/preop/odoo_writer.py`), que pasa siempre por la cola de `preoperacional` (estado, reintentos, idempotencia). No agregar escrituras directas desde handlers o endpoints: si una funcionalidad nueva necesita escribir, debe seguir el mismo patrón (registro local `pendiente` → worker → Odoo).
- **Separar lógica pura de E/S.** `ticket_state.py` (reglas de estado) y `watcher.decide()` (qué hacer con una vigilancia) no hacen llamadas de red ni a la base de datos: reciben datos ya cargados y devuelven una decisión. La E/S (Odoo, Telegram, SQLite) vive en `odoo_client.py`, `telegram_app.py`, `watch_store.py`. Mantén esa frontera al agregar funcionalidad: si una función nueva necesita decidir algo, piensa primero si puede ser pura y probarse sin mocks.
- **Inyección de dependencias simple.** `telegram_app.build_application()` construye un único objeto `Services` (dataclass) con todas las dependencias (`settings`, `odoo`, `access`, `store`, `watcher`) y lo cuelga en `application.bot_data`. Los handlers lo recuperan con `_services(context)`. Sigue ese mismo patrón para nuevas dependencias en vez de usar variables globales o `get_settings()` disperso por el código.
- **Nunca confiar en el cuerpo de la petición para la identidad.** La Mini App (`app/web.py`) obtiene el `user_id` únicamente de `initData` validado con HMAC (`app/webapp_auth.py`). Si agregas un endpoint nuevo que actúa en nombre de un usuario, exige y valida `initData`; no aceptes un `user_id` en el body o en query params.
- **Texto en Telegram siempre HTML-escapado.** `app/formatting.py` usa `html.escape()` en cualquier dato que venga de Odoo (nombre del pasajero, rutas, etc.) antes de interpolarlo en un mensaje `parse_mode="HTML"`. Sigue el mismo patrón para evitar inyección de HTML en el chat.
- **Secretos como `SecretStr`.** `ODOO_PASSWORD`, `TELEGRAM_BOT_TOKEN` y `TELEGRAM_WEBHOOK_SECRET` son `pydantic.SecretStr` en `app/config.py`: no se imprimen por accidente en logs o `repr()`. Cualquier variable de entorno nueva que sea un secreto debe declararse igual.
- **Comentarios en español, explicando el "por qué".** El estilo del proyecto usa docstrings y comentarios cortos en español que explican una decisión no obvia (p. ej. por qué se ignora el host del QR, por qué el aviso al pasajero se intenta antes que el aviso al validador). Evita comentarios que repitan lo que el código ya dice.
- **Un solo SQL para SQLite y PostgreSQL.** `app/watch_store.py` usa SQLAlchemy Core (sin ORM) con `text()` y parámetros nombrados (`:nombre`), nunca f-strings ni `%s`: ese mismo SQL debe seguir funcionando igual contra `sqlite:///...` (dev/tests) y `postgresql+psycopg2://...` (Docker/producción). Antes de usar una función específica de un motor (p. ej. `RETURNING`, tipos de SQLite como `AUTOINCREMENT`), comprueba que el otro motor la soporte igual o evítala.

## Variables de entorno nuevas

Si agregas una variable:
1. Añádela a `app/config.py` (`Settings`), con tipo y valor por defecto cuando tenga sentido.
2. Documéntala en `.env.example` con un comentario breve.
3. Añádela a la tabla de variables de `README.md`.
4. Si es un secreto, usa `SecretStr`.

## Agregar una opción al menú del bot

El teclado principal de @OperativosViacoturBot es plegable (`is_persistent=False`, Telegram lo muestra detrás de su botón de teclado) y solo tiene **📋 Formularios** y **📍 Enviar ubicación**. El panel de 📋 Formularios (`formularios_inline`) lista los formularios de la tupla `FORMULARIOS` de `app/telegram_app.py`, en su orden, y al final siempre **📷 Escanear QR** y **🔴 Finalizar**. Ejemplo: Formularios → ✅ Preoperacional → 📷 Escanear QR → 🔴 Finalizar.

1. Servir la Mini App desde este servicio (ruta FastAPI + `app/static/<nombre>.html`) y validar `initData` en su API (`app.webapp_auth.validate_init_data_user`). Seguir la identidad visual del preoperacional (cabecera con `/static/viacotur-logo.png`, paleta azul, pasos con barra de progreso).
2. Agregar una `MenuOption(label, open_text, prompt, help, url)` a `FORMULARIOS` en la posición deseada. Aparece sola en el panel (antes de Escanear QR y Finalizar), en la ayuda de `/start` y en el manejo.
3. Opcional: un comando (`CommandHandler` + `BotCommand` en `BOT_COMMANDS`) que llame a `_open_option`.
4. Tablas nuevas: van en la BD del bot (`app/preop/models.py` o un módulo hermano con la misma `Base`); `create_all` las crea al arrancar. Para **cambiar** columnas de tablas existentes hace falta Alembic.
5. Las Mini Apps se abren siempre con botón **inline** `web_app`: desde un botón de teclado llegan sin `initData`.
6. Cubrir el orden y la apertura en `tests/test_handlers.py`.

## Agregar un nuevo bot operativo al mismo patrón

La intención de este repositorio es centralizar varios bots de Telegram conectados a Odoo (ver "Por qué existe este proyecto" en `README.md`). Un bot nuevo **no se mezcla dentro de `app/`** de este proyecto: se crea como una carpeta hermana independiente, replicando esta misma estructura:

- Carpeta propia bajo `/opt/odoo/custom/<nombre_del_bot>`, con su propio `app/`, `tests/`, `requirements.txt`, `.env.example`, `Dockerfile` y `docker-compose.yml`.
- `.venv` propio si se corre sin Docker (no compartido entre bots). Con Docker no hace falta `.venv` en el servidor.
- Puerto propio en `127.0.0.1` (este validador usa `8011`; `telegram_boarding_bot` usa `8010`).
- Base de datos propia si la necesita (contenedor `db` propio en su `docker-compose.yml`, como aquí), nunca compartir el contenedor de Postgres de otro bot.
- Si necesita hablar con otro bot del conjunto, usar HTTP interno con un token compartido leído de Odoo (ver `app/passenger_notifier.py` y `bot.telegram.config.internal_token` como ejemplo), nunca acceso directo a la base de datos de otro bot.
- Subdominio propio al desplegar (ver sección de despliegue en `README.md`); unidad systemd propia solo si se quiere administrar con `systemctl` (opcional con Docker).

## Qué no se versiona

`.gitignore` ya excluye `.venv/`, `.env`, `run/` (logs, SQLite de vigilancias, URL del túnel) y cachés de Python/pytest. `.dockerignore` excluye lo mismo del contexto de build de Docker. Nunca commitear:
- `.env` (secretos reales).
- `run/watches.sqlite3` u otro estado runtime.
- Un `.venv` (es específico de la ruta de la máquina donde se creó; ver arriba).
- El volumen `pgdata` de PostgreSQL (lo gestiona Docker, no es un archivo del repo).

Al rotar un secreto (token de Telegram, API key de Odoo, webhook secret, `POSTGRES_PASSWORD`), actualízalo en `.env` en cada entorno (QA y producción son archivos `.env` independientes) y reinicia el servicio (`docker compose up -d --build`); no hay recarga en caliente de configuración en este bot (a diferencia de `telegram_boarding_bot`, que sí expone `/internal/reload-config`).

## Versionado

El proyecto usa [versionado semántico](https://semver.org/lang/es/). Al cerrar un cambio que vaya a desplegarse:

1. Añade una entrada en `CHANGELOG.md` bajo `[Sin publicar]` (o crea la sección de la versión si ya se va a liberar).
2. Si el cambio es `MINOR` o `MAJOR`, actualiza también `version=` en `app = FastAPI(...)` (`app/main.py`).
3. Al liberar, mueve las entradas de `[Sin publicar]` a una sección `## [X.Y.Z] - AAAA-MM-DD` y crea un tag de git `vX.Y.Z` (cuando el proyecto esté versionado en git).

Criterio: `PATCH` para correcciones sin cambiar comportamiento visible; `MINOR` para capacidades nuevas compatibles hacia atrás (p. ej. agregar soporte de PostgreSQL manteniendo SQLite como default); `MAJOR` para romper compatibilidad (p. ej. cambiar el nombre de una variable de entorno sin mantener la anterior, cambiar el contrato de un endpoint).
