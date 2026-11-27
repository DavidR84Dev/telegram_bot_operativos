# Panel Admin: bot de Telegram con Mini App para administradores

Bot **nuevo e independiente** (token propio de BotFather) con una Mini App, el **PORTAL**, para los
administradores. Está en la línea del panel de referencia `docs/viacotur_admin_bot.md`, pero adaptado a las
tablas de este proyecto. Código en `app/admin/`; la página, en `app/static/admin.html`.

- Comparte la **BD y los modelos** (`app/preop/models.py`) con Operativos, pero corre en **su propio proceso y
  contenedor**: `python -m app.admin` (FastAPI + `python-telegram-bot` en polling).
- Nada de lo que hace escribe en Odoo.

---

## 1. Qué hace

`/start` responde **«Selecciona un formulario:»** con el botón Web App **🖥️ PORTAL**, y el bot también configura
el botón de menú (**PORTAL**) junto al campo de mensaje. Quien no esté en `ADMIN_TELEGRAM_IDS` recibe un rechazo
amable con su ID de Telegram para que pida el acceso.

La Mini App tiene seis pestañas:

| Pestaña | Función |
|---|---|
| 🚌 **Vehículos** | KPIs (activas, vencidas sin liberar, liberadas hoy) y tabla de `reservas_vehiculo`: conductor, vehículo/placa, IDs de Odoo, ID de Telegram, preoperacional, reservado y vence. Buscador por conductor, placa o ID. **Liberar** reutiliza `app/preop/reservas._liberar` con motivo `manual`: la fila pasa a `reservas_historial`. Pide confirmación. |
| 👥 **Empleados** | Búsqueda **de solo lectura** en `empleados` (nombre, documento, PIN/Telegram o ID de Odoo) con puesto, contrato, responsable y el vehículo reservado, si lo tiene. Más una búsqueda de solo lectura en `flota`. El documento solo existe para quienes se registraron con 📝 Registro (la copia local de empleados no lo guarda). |
| 📝 **Usuarios** | Lista y búsqueda de `registros_conductor`. **Eliminar** borra **solo la fila local**: no escribe en Odoo y el PIN no se toca. La UI avisa que, si el PIN sigue en Odoo, la persona conserva el acceso. |
| 🗄️ **CSV BD** | Dos cargas de CSV con el mismo flujo (vista previa, errores por fila, confirmación, «reemplazar» y lista con activar/desactivar): **a)** estaciones de combustible con ubicación (§ 5) y **b)** restaurantes y locales autorizados de Gastos Operativos, con una consulta de solo lectura del cotejo de los gastos (§ 5b). |
| ⚙️ **Parámetros** | Edición de parámetros operativos del `.env` (lista blanca en código). Ver § 6. |
| 📜 **Auditoría** | Últimas acciones de los administradores. |

---

## 2. Crear el bot en BotFather

1. En Telegram abre **@BotFather** y envía `/newbot`. Elige nombre y usuario (por ejemplo
   `viacotur_admin_bot`; cuida que el nombre visible no tenga errores de digitación). Guarda el **token**.
2. **Mini App / botón de menú.** El código ya lo configura por API al arrancar (`setChatMenuButton` con
   `MenuButtonWebApp`, texto «PORTAL», apuntando a `ADMIN_WEBAPP_BASE_URL`). Si prefieres hacerlo a mano:
   `/setmenubutton` → elige el bot → pega la URL HTTPS del panel → título `PORTAL`.
3. (Opcional) **OPEN APP** en la ficha del bot: `/newapp` → elige el bot → pega la misma URL.
4. **Dominio de la Mini App:** `/setdomain` (o *Bot Settings → Domain*) con el dominio del panel, si el cliente
   de Telegram lo pide. La URL debe ser **HTTPS público** (túnel en pruebas, dominio en producción).
5. Obtén tu ID numérico de Telegram (p. ej. escribiendo `/start` al bot: si no estás en la lista, el rechazo
   muestra tu ID) y agrégalo a `ADMIN_TELEGRAM_IDS`.

---

## 3. Variables (`.env`)

Todas son opcionales para no romper Operativos; sin `ADMIN_BOT_TOKEN` el panel simplemente no arranca.

| Variable | Descripción |
|---|---|
| `ADMIN_BOT_TOKEN` | Token de BotFather del bot admin (distinto del de Operativos). |
| `ADMIN_TELEGRAM_IDS` | Lista blanca de IDs de Telegram separados por coma. **Vacía = nadie entra.** |
| `ADMIN_WEBAPP_BASE_URL` | URL HTTPS pública del panel (la del botón PORTAL). |
| `ADMIN_LISTEN_HOST` / `ADMIN_LISTEN_PORT` | Dónde escucha el proceso (por defecto `127.0.0.1:8012`; en Docker, `0.0.0.0`). |
| `ADMIN_ENV_FILE` | `.env` que edita la pestaña Parámetros. Por defecto `/srv/.env`. |
| `ADMIN_AUTH_MAX_AGE_SECONDS` | Antigüedad máxima del `initData` (por defecto 14400 = 4 h; el panel se deja abierto un rato). |

El panel también lee del mismo `.env` lo que ya usa Operativos (`DATABASE_URL`, `LOG_LEVEL`...).

---

## 4. Despliegue

El servicio `admin` de `docker-compose.yml` usa el mismo build y `env_file` que `app`:

- Comando `python -m app.admin`, puerto `127.0.0.1:8012:8012` (solo loopback: el túnel o nginx hace el HTTPS).
- Depende de `db` y usa el mismo `DATABASE_URL` de Postgres.
- Monta `./.env:/srv/.env` (**lectura y escritura**, para editar parámetros) y `./run:/srv/run` (respaldos).

```bash
docker compose up -d --build admin
docker compose logs --tail 20 admin     # debe decir "Panel Admin: polling activo"
```

Notas de operación:

- **Permisos del `.env`:** el contenedor corre como `appuser` (uid 1000). El `.env` del host debe poder
  escribirlo ese usuario (`chown 1000 .env` o `chmod 664 .env`); si no, guardar parámetros falla con un error.
- Es un bind mount de **un solo archivo**: el panel escribe en el mismo archivo (nunca con `rename`, que falla
  con `EBUSY`). Si editas el `.env` con un editor que lo reemplaza (nuevo inodo), reinicia el servicio `admin`
  para que vuelva a ver el archivo.
- El panel y Operativos usan **tokens distintos**: pueden correr a la vez sin el error
  `Conflict: terminated by other getUpdates request`.
- Para exponerlo en local con un túnel, ver `docs/ENTORNO_LOCAL_WINDOWS.md` § 5.2.

---

## 5. CSV BD, sección a): estaciones de combustible

UTF-8 (también se acepta el CSV de Excel en Windows-1252), con encabezado y separado por `;` o `,`:

| Columna | Obligatoria | Descripción |
|---|---|---|
| `nombre` | sí | Nombre de la estación. |
| `nit` | no | Solo dígitos tras normalizar; llave de actualización. |
| `odoo_partner_id` | no | Contacto (`res.partner`) de Odoo. |
| `latitud`, `longitud` | no, pero **juntas** | Grados decimales, rango -90..90 y -180..180. Con `;` como separador se acepta también la coma decimal (`6,2442`); con `,` como separador la coma decimal debe ir entre comillas. |
| `ciudad`, `direccion` | no | Texto libre. |

Plantilla: `docs/proveedores_combustible_ejemplo.csv`. Se actualiza por NIT o, sin NIT, por nombre normalizado. Un
CSV sin ubicación **no borra** la ubicación ya guardada.

Flujo en la UI: elegir archivo → **Vista previa** (filas válidas, errores por fila, nuevas y actualizadas; no
escribe nada) → **Confirmar carga**. Las filas con error no se cargan. Con **«Reemplazar»** se desactivan las
estaciones activas que no vengan en el archivo y, por seguridad, **el archivo no puede tener errores** (faltarían
filas y se desactivarían de más); pide confirmación. La lista permite activar o desactivar cada estación; una
inactiva no aparece en el formulario de ⛽ Combustible.

El CLI sigue funcionando: `python -m app.combustible.proveedores cargar|listar|desactivar` (ver `docs/COMBUSTIBLE.md`).

Las utilidades de CSV comunes a las dos secciones (decodificación UTF-8/Windows-1252, separador `;` o `,`, coma decimal,
filas con más columnas que encabezados) están en `app/csv_util.py`.

---

## 5b. CSV BD, sección b): restaurantes y locales autorizados

Lista de locales (restaurantes, hoteles...) de **Gastos Operativos** (`app/gastos_operativos/locales.py`, tabla
`locales_autorizados`). Su ubicación sirve para cotejar la del gasto con el local autorizado más cercano
(`docs/GASTOS_OPERATIVOS.md` § 12): el cotejo **solo se almacena**, no va a Odoo, no se muestra en el formulario ni
bloquea nada.

UTF-8 (también Windows-1252), con encabezado y separado por `;` o `,`:

| Columna | Obligatoria | Descripción |
|---|---|---|
| `nombre` | sí | Nombre del local. |
| `nit` | no | Solo dígitos tras normalizar; llave de actualización. |
| `categoria` | no | Nombre de un tipo de `GASTOS_OPERATIVOS_TIPOS` (p. ej. Alimentación, Hospedaje), **sin distinguir tildes ni mayúsculas**. Vacía = el local vale para todos los tipos. Una categoría que no sea un tipo configurado es un error de la fila (el mensaje lista los tipos válidos). |
| `latitud`, `longitud` | **sí** | Grados decimales, rango -90..90 y -180..180; mismas reglas de coma decimal que las estaciones. |
| `ciudad`, `direccion` | no | Texto libre. |

Plantilla: `docs/locales_autorizados_ejemplo.csv`. Se actualiza **por NIT** o, sin NIT, **por nombre normalizado más
categoría** (el mismo nombre con otra categoría es otro local). El panel valida las categorías con los tipos que haya
en el `.env` (`ADMIN_ENV_FILE`) en ese momento; sin la clave usa los tipos por defecto del código.

La UI es la de las estaciones: **Vista previa** (no escribe nada) → **Confirmar carga**; las filas con error no se
cargan; con **«Reemplazar»** se desactivan los locales activos que no vengan y el archivo **no puede tener errores**.
La lista busca por nombre, ciudad, categoría o NIT y permite activar/desactivar; un local inactivo deja de usarse en
el cotejo de los gastos nuevos.

CLI: `python -m app.gastos_operativos.locales cargar ARCHIVO.csv [--reemplazar]`, `listar` y `desactivar ID...`.

**Cotejo de gastos (solo lectura).** Debajo de la lista, una tabla con filtros de fecha del gasto y categoría muestra,
por cada línea de gasto, la ubicación recibida, el local más cercano de su categoría y la distancia
(`GET /api/admin/gastos-cotejo`). «Sin locales» = se cotejó pero no había locales aplicables; «Sin cotejar» = el envío
aún no recibió su ubicación o el cotejo falló (ver el log).

---

## 6. Parámetros editables (`.env`)

La **lista blanca está en código** (`app/admin/envfile.py`, `PARAMETROS`), nunca en el `.env`:

| Grupo | Claves |
|---|---|
| Operación | `LOG_LEVEL`, `POLL_INTERVAL_SECONDS`, `WATCH_TTL_HOURS`, `EMPLOYEE_CACHE_SECONDS`, `MASTER_SYNC_INTERVAL_SECONDS` |
| Colas a Odoo | `PREOP_WORKER_INTERVAL_SECONDS`, `PREOP_MAX_ATTEMPTS`, `PREOP_SENDING_LEASE_SECONDS`, `PREOP_AUTH_MAX_AGE_SECONDS` |
| Correcciones | `CORRECCIONES_DIAS_MAXIMOS` (antigüedad máxima, en días, de los gastos rechazados y Post-operacionales pendientes que se pueden corregir; entero de 1 a 365, por defecto 30) |
| Negocio | `PRUEBA_RUTA_ACUERDOS`, `COMBUSTIBLE_GASTOS_PRODUCT_ID`, `GASTOS_OPERATIVOS_TIPOS`, `GASTOS_OPERATIVOS_TOPES`, `ODOO_API_COMPANY_IDS`, `ODOO_API_PAGE_SIZE` |
| GPS | `GPS_FLOTA_URL`, `GPS_FLOTA_TIMEOUT_SECONDS` |
| Secreto (solo escritura) | `GPS_FLOTA_API_KEY`: nunca se muestra su valor, solo «configurado» o «vacío»; se puede escribir uno nuevo o vaciar. |

**Fuera de la lista y sin edición:** tokens de Telegram, `ADMIN_*`, `DATABASE_URL`, `POSTGRES_PASSWORD`,
credenciales de Odoo y URLs base.

Validación: el tipo sale del campo en `Settings` (enteros > 0, decimales con **punto** > 0, texto). Además,
`LOG_LEVEL` solo admite sus cinco niveles, `CORRECCIONES_DIAS_MAXIMOS` debe estar entre 1 y 365 (ver `docs/CORRECCIONES.md` § 2; el bot principal toma el valor al recrear el contenedor, como los demás parámetros), las URL deben ser `http(s)://` (o vacías) y `ODOO_API_COMPANY_IDS` es
una lista de números, y `GASTOS_OPERATIVOS_TIPOS` debe ser `Nombre:product_id` separados por `;` (sin nombres repetidos ni ids no numéricos; el valor se guarda normalizado, p. ej. `Alimentación:1;Otros:6`; ver `docs/GASTOS_OPERATIVOS.md` § 3). `GASTOS_OPERATIVOS_TOPES` es `Nombre:valor` en COP separados por `;` (enteros > 0, vacío = sin topes): cada nombre debe existir en los tipos (sin distinguir tildes ni mayúsculas) y se guarda con el nombre de los tipos, p. ej. `Alimentación:50000;Hospedaje:150000`; esa validación cruzada se hace al guardar, con los tipos que quedarían en el `.env` (si cambias tipos y topes a la vez se comprueban juntos; no se puede quitar un tipo que aún tiene tope). La pestaña lo muestra como **una fila por tipo configurado con un campo numérico** (vacío = sin tope). Ver `docs/GASTOS_OPERATIVOS.md` § 3b. Se rechazan comillas, `#`, `$`, `\` y saltos de línea. Todo o nada: si un valor es
inválido no se escribe ninguno.

Al guardar:

1. Respaldo en `run/env-backups/.env.<AAAAMMDDTHHMMSS_µs>Z` junto al `.env` (se conservan los **últimos 20**;
   los respaldos contienen secretos: `chmod 600`).
2. Solo se modifican las líneas de las claves cambiadas; comentarios y orden quedan igual. Una clave que no
   existe se añade al final. Un valor igual al actual no cambia nada ni crea respaldo.
3. Se escribe **en el mismo archivo** (sin `rename`).
4. Se audita la **clave** cambiada; de un secreto, solo la clave.

### Reinicio

**Los cambios no se aplican solos.** Operativos lee el `.env` al arrancar; después de guardar:

```bash
docker compose restart app
```

(Si cambiaste `LOG_LEVEL` y quieres verlo en el propio panel: `docker compose restart admin`.) Para deshacer un
cambio, copia el respaldo de `run/env-backups/` sobre `.env` y reinicia.

---

## 7. Seguridad y auditoría

- **Todos** los endpoints `/api/admin/*` exigen `X-Telegram-Init-Data` con firma HMAC-SHA256 válida **del token
  del bot admin** (reutiliza `app/webapp_auth.py`): firma mala, alterada, caducada o del bot de Operativos → **401**.
- Además, el ID de Telegram debe estar en `ADMIN_TELEGRAM_IDS` → si no, **403**. Lista vacía = nadie.
- La página `/admin` (y `/`) es pública pero no contiene datos; todo dato viaja por la API.
- Las acciones destructivas (liberar, eliminar, desactivar, reemplazar, vaciar un secreto) piden confirmación en
  la UI.
- **Datos personales:** la pestaña Usuarios muestra documento y correo (Ley 1581 de 2012); limita la lista de
  administradores. La auditoría no guarda documento ni correo.
- **Auditoría:** tabla `admin_auditoria` (`created_at`, `telegram_user_id`, `nombre`, `accion`, `objetivo`,
  `detalle`). Acciones: `reserva.liberar`, `usuario.eliminar`, `estaciones.cargar`, `estacion.activar`,
  `estacion.desactivar`, `locales.cargar`, `local.activar`, `local.desactivar`, `parametro.cambiar`. El detalle nunca lleva valores de secretos ni de parámetros.

  ```sql
  SELECT created_at, telegram_user_id, accion, objetivo, detalle FROM admin_auditoria ORDER BY id DESC LIMIT 50;
  ```

---

## 8. Rutas

| Ruta | Descripción |
|---|---|
| `GET /`, `GET /admin` | HTML de la Mini App (sin caché) |
| `GET /health` | Salud (sin auth) |
| `GET /api/admin/me` | Identidad del administrador |
| `GET /api/admin/reservas?q=` · `POST /api/admin/reservas/{id}/liberar` | Reservas y liberación manual |
| `GET /api/admin/empleados?q=` · `GET /api/admin/flota?q=` | Búsquedas de solo lectura |
| `GET /api/admin/usuarios?q=` · `DELETE /api/admin/usuarios/{id}` | Registros de conductor (solo fila local) |
| `GET /api/admin/estaciones?q=&estado=` | Lista de estaciones |
| `POST /api/admin/estaciones/previsualizar` · `POST .../cargar` | Vista previa y carga del CSV (`{csv, reemplazar}`) |
| `POST /api/admin/estaciones/{id}/activo` | Activar o desactivar |
| `GET /api/admin/locales?q=&estado=` | Lista de locales autorizados y las categorías (tipos de gasto) válidas |
| `POST /api/admin/locales/previsualizar` · `POST .../cargar` | Vista previa y carga del CSV (`{csv, reemplazar}`) |
| `POST /api/admin/locales/{id}/activo` | Activar o desactivar |
| `GET /api/admin/gastos-cotejo?desde=&hasta=&categoria=&limite=` | Solo lectura: cotejo de cada gasto con el local más cercano (fechas `AAAA-MM-DD`) |
| `GET /api/admin/parametros` · `PUT /api/admin/parametros` | Lectura y escritura (`{cambios: {CLAVE: valor}}`) |
| `GET /api/admin/auditoria?limite=` | Bitácora |

## 9. Pruebas

`tests/test_admin.py` (auth 401/403 en todos los endpoints, reservas, usuarios, CSV, parámetros con `.env`
temporal, auditoría y bot). Más `tests/test_combustible.py` (CSV con ubicación, columnas nuevas y proveedores con
ubicación en el endpoint de combustible) y `tests/test_locales_topes.py` (CSV de locales, endpoints de locales y de cotejo
con 401/403, cotejo, topes y su parámetro).
