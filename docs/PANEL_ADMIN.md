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
| ⛽ **Estaciones** | Carga de CSV de estaciones autorizadas con ubicación (vista previa, errores por fila y confirmación), opción «reemplazar» y lista con activar/desactivar. Ver § 5. |
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

## 5. Estaciones de combustible: formato del CSV

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

---

## 6. Parámetros editables (`.env`)

La **lista blanca está en código** (`app/admin/envfile.py`, `PARAMETROS`), nunca en el `.env`:

| Grupo | Claves |
|---|---|
| Operación | `LOG_LEVEL`, `POLL_INTERVAL_SECONDS`, `WATCH_TTL_HOURS`, `EMPLOYEE_CACHE_SECONDS`, `MASTER_SYNC_INTERVAL_SECONDS` |
| Colas a Odoo | `PREOP_WORKER_INTERVAL_SECONDS`, `PREOP_MAX_ATTEMPTS`, `PREOP_SENDING_LEASE_SECONDS`, `PREOP_AUTH_MAX_AGE_SECONDS` |
| Negocio | `PRUEBA_RUTA_ACUERDOS`, `COMBUSTIBLE_GASTOS_PRODUCT_ID`, `ODOO_API_COMPANY_IDS`, `ODOO_API_PAGE_SIZE` |
| GPS | `GPS_FLOTA_URL`, `GPS_FLOTA_TIMEOUT_SECONDS` |
| Secreto (solo escritura) | `GPS_FLOTA_API_KEY`: nunca se muestra su valor, solo «configurado» o «vacío»; se puede escribir uno nuevo o vaciar. |

**Fuera de la lista y sin edición:** tokens de Telegram, `ADMIN_*`, `DATABASE_URL`, `POSTGRES_PASSWORD`,
credenciales de Odoo y URLs base.

Validación: el tipo sale del campo en `Settings` (enteros > 0, decimales con **punto** > 0, texto). Además,
`LOG_LEVEL` solo admite sus cinco niveles, las URL deben ser `http(s)://` (o vacías) y `ODOO_API_COMPANY_IDS` es
una lista de números. Se rechazan comillas, `#`, `$`, `\` y saltos de línea. Todo o nada: si un valor es
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
  `estacion.desactivar`, `parametro.cambiar`. El detalle nunca lleva valores de secretos ni de parámetros.

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
| `GET /api/admin/parametros` · `PUT /api/admin/parametros` | Lectura y escritura (`{cambios: {CLAVE: valor}}`) |
| `GET /api/admin/auditoria?limite=` | Bitácora |

## 9. Pruebas

`tests/test_admin.py` (auth 401/403 en todos los endpoints, reservas, usuarios, CSV, parámetros con `.env`
temporal, auditoría y bot). Más `tests/test_combustible.py` (CSV con ubicación, columnas nuevas y proveedores con
ubicación en el endpoint de combustible).
