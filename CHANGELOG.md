# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/).
Versionado: [SemVer](https://semver.org/lang/es/) (`MAJOR.MINOR.PATCH`).

## [Sin publicar]

### Agregado
- **📝 Registro de conductores nuevos (2026-11-24):** quien escribe al bot sin tener su id de Telegram en el PIN de un empleado ya no recibe "Acceso no autorizado": se le ofrece 📝 Registro (o `/registro`), antes del preoperacional. Pide nombre completo, cédula o documento y correo (uno por mensaje, con validación; `/cancelar` en cualquier paso) y los guarda con el id de Telegram en la nueva tabla `registros_conductor`. Valida al conductor en `/api/empleados` por número de documento: si no existe responde "Conductor no registrado en Odoo. Por favor comunicarse con el área de Talento humano"; si existe envía `{id, pin}` a `/api/empleados/register` y responde "Usuario correctamente registrado en Odoo…". No sobrescribe el PIN de un empleado que ya tiene otro id de Telegram. Solo los usuarios sin registro completado ven la opción. Requiere `ODOO_API_LOGIN`/`ODOO_API_PASSWORD`.
- **Preoperacional sin señal (2026-11-24):** si el envío falla por falta de señal, la Mini App guarda el formulario en el teléfono (IndexedDB) y lo reenvía sola al volver la señal, o al reabrirla. Se acepta como máximo **1 hora después de perder la señal** (anclado a la hora de apertura firmada por Telegram; tope de 2 h desde la apertura). Fuera de la ventana el servidor responde 410 y lo guarda solo como auditoría, sin Odoo, sin reserva y sin aviso al conductor. Nueva tabla `preop_envios_diferidos` con la trazabilidad (apertura, pérdida de señal, recepción, retraso, huella SHA-256). Ver `docs/PREOPERACIONAL.md` § Envío sin señal.
- **Reserva del vehículo por jornada y botón 🔴 Finalizar:** tabla temporal `reservas_vehiculo`: una placa por conductor y un conductor por placa. El preoperacional reserva el vehículo; los demás lo ven como NO DISPONIBLE (rojo) y el servidor rechaza su envío. Se libera con 🔴 Finalizar (con confirmación y aviso al jefe inmediato por Telegram), a las 24 h de su registro inicial o con `python -m app.preop.reservas --liberar PLACA`. Cada liberación queda en `reservas_historial`.

### Cambiado
- **Preoperacional, decimales y obligatorios (2026-11-24, tarde):**
  - Decimales con punto, porque Odoo solo lo admite en los `float`: la Mini App cambia la coma por punto mientras se escribe (`5,5` → `5.5`) y muestra los números con punto decimal y miles con espacio (`125 430 km`); el servidor convierte a número un texto con coma (`"5,5"`) antes de validar, en vez de rechazarlo.
  - Labrado y presión obligatorios en las llantas 1–4 y la de repuesto (y en las internas 5 y 6 si el vehículo las tiene), en la Mini App y en el servidor.
  - El aviso de pendientes ahora también lista las medidas de llantas que faltan o no son válidas, y cada una lleva a su campo; antes "Siguiente" podía quedar deshabilitado sin explicación. El estado del botón se recalcula al escribir una medida y al responder "¿Tiene llantas internas?".
  - Se desactiva a propósito "✔ Marcar pestaña conforme": el conductor revisa cada ítem.
  - Nueva guía `docs/GUIA_NUEVOS_BOTS.md` con estas convenciones para los próximos bots.
- **Preoperacional, ajustes del 2026-11-24:**
  - «📷 Tomar foto» abre la cámara trasera dentro del formulario (`getUserMedia`): Telegram para Android ignoraba `capture` y abría la galería.
  - Labrado máximo por tipo de vehículo para evitar errores de digitación: **Buses 20 mm** (bus, buseta, minibus) y **Camioneta 15 mm** (los demás). Validado en la Mini App y en el servidor.
  - Solo se puede hacer el preoperacional de un vehículo **DISPONIBLE**. Un vehículo reservado, incluso a nombre del mismo conductor, queda bloqueado en la Mini App y el servidor lo rechaza (409). Sin vehículos disponibles, el formulario no deja entrar y explica por qué.
- **Preoperacional, ajustes del 2026-11-23 (noche):**
  - Rines/Llantas: el labrado no puede enviarse en 0 mm (Mini App y servidor); vacío sigue siendo "no medido" y la presión admite 0.
  - Resumen: la tarjeta "Observaciones y estado final" siempre visible (texto escrito en Evidencia o "Sin observaciones."); "Siguiente" cierra el teclado para que Android confirme la última palabra escrita.
- **Preoperacional, ajustes del 2026-11-23 (tarde):**
  - Sugerencia "Último registrado" del odómetro leída en vivo de `fleet.vehicle.odometer_post`; si está en 0 (campo sin `@api.depends`), `km_end` del último Pos-Operacional. La misma cifra valida el envío.
  - "Testigos del tablero" pasa a casilla, como "Medidor de aire".
  - Los ítems de 3 opciones (con "No aplica") son obligatorios: "Siguiente" no pasa a Evidencia sin ellos y el servidor los exige.
  - Foto de testigos desde la cámara o la galería del celular.
  - Observaciones visibles en el Resumen, debajo de la información general.
- **Formulario Preoperacional reorganizado según el recorrido de `@viacoturform_bot` (2026-11-23, `docs/mapa_viacotur_form.md` §8–9):**
  - Catálogo (`app/preop/checklist.py`) con pestañas **Documentos · Rines/Llantas · Equipo · Vehículo**, el orden real de los ítems, el criterio de no conforme como dato y las llantas agrupadas por posición (5 y 6 solo con llantas internas). Mismos campos y opciones de Odoo que antes.
  - "Testigos del tablero" pasa a la pestaña Equipo; "Odómetro actual" y número interno visibles en el paso 1.
  - Se permite enviar con ítems sin marcar o no conformes y sin la foto de testigos: el resumen los muestra como alertas (✔ / ! / N/A, 0/1) y permite saltar al ítem para corregir.
  - Diseño móvil: pestañas fijas, un solo scroll, áreas táctiles de 44 px y teclado numérico (`inputmode`) en odómetro, labrado y presión.
- **Parámetros del Preoperacional (definidos por el usuario):**
  - Paso 1 como la referencia: contrato → vehículo (tarjetas de los extractos aprobados, uno o varios) → odómetro, que debe ser **mayor o igual** al último registrado en Odoo (validado en la Mini App y en el servidor).
  - Casillas y radios como `@viacoturform_bot`: casilla sin marcar = `no conforme`; cámaras con 3 opciones al campo `camaras`; sin las preguntas `llanta_rep_pre_act` ni `camaras_new`.
  - Alerta de labrado con el mínimo del acuerdo (`labrado_camioneta` / `labrado_buses` según el tipo de vehículo); nueva tabla `acuerdos` sincronizada desde `/api/acuerdos`.
  - Observaciones sin texto se envían en blanco; ya no se anotan ítems sin responder.
  - Aviso por Telegram al jefe inmediato cuando hay no conformes o labrado bajo el mínimo.

### Agregado
- **Preoperacional como bot de referencia (`@viacoturform_bot`)**:
  - Menú de @OperativosViacoturBot: **📋 Formularios** (lista inline, ✅ Preoperacional primero), **📷 Escanear QR** y **📍 Enviar ubicación**; comando `/formularios`.
  - Mini App por pasos (Información → Verificación → Evidencia → Resumen) con barra de progreso e identidad VIACOTUR (logo y paleta).
  - Vehículo tomado y bloqueado desde el **extracto aprobado** del conductor; sin extracto aprobado, pantalla de bloqueo con instrucciones.
  - Foto de testigos del tablero (comprimida en el teléfono), guardada en `preop_fotos` y enviada a `testigos_img` de Odoo.
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

### Agregado
- **Reserva del vehículo por jornada y botón 🔴 Finalizar:** tabla temporal `reservas_vehiculo`: una placa por conductor y un conductor por placa. El preoperacional reserva el vehículo; los demás lo ven como NO DISPONIBLE (rojo) y el servidor rechaza su envío. Se libera con 🔴 Finalizar (con confirmación y aviso al jefe inmediato por Telegram), a las 24 h de su registro inicial o con `python -m app.preop.reservas --liberar PLACA`. Cada liberación queda en `reservas_historial`.

### Cambiado
- **Preoperacional, ajustes del 2026-11-23 (tarde):**
  - Sugerencia "Último registrado" del odómetro leída en vivo de `fleet.vehicle.odometer_post`; si está en 0 (campo sin `@api.depends`), `km_end` del último Pos-Operacional. La misma cifra valida el envío.
  - "Testigos del tablero" pasa a casilla, como "Medidor de aire".
  - Los ítems de 3 opciones (con "No aplica") son obligatorios: "Siguiente" no pasa a Evidencia sin ellos y el servidor los exige.
  - Foto de testigos desde la cámara o la galería del celular.
  - Observaciones visibles en el Resumen, debajo de la información general.
- El servicio pasa a responder como **@OperativosViacoturBot** (nuevo `TELEGRAM_BOT_TOKEN`), en lugar de @QRLectorBot, para que el nombre coincida con su función de bot operativo.
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

### Agregado
- **Reserva del vehículo por jornada y botón 🔴 Finalizar:** tabla temporal `reservas_vehiculo`: una placa por conductor y un conductor por placa. El preoperacional reserva el vehículo; los demás lo ven como NO DISPONIBLE (rojo) y el servidor rechaza su envío. Se libera con 🔴 Finalizar (con confirmación y aviso al jefe inmediato por Telegram), a las 24 h de su registro inicial o con `python -m app.preop.reservas --liberar PLACA`. Cada liberación queda en `reservas_historial`.

### Cambiado
- **Preoperacional, ajustes del 2026-11-23 (tarde):**
  - Sugerencia "Último registrado" del odómetro leída en vivo de `fleet.vehicle.odometer_post`; si está en 0 (campo sin `@api.depends`), `km_end` del último Pos-Operacional. La misma cifra valida el envío.
  - "Testigos del tablero" pasa a casilla, como "Medidor de aire".
  - Los ítems de 3 opciones (con "No aplica") son obligatorios: "Siguiente" no pasa a Evidencia sin ellos y el servidor los exige.
  - Foto de testigos desde la cámara o la galería del celular.
  - Observaciones visibles en el Resumen, debajo de la información general.
- Renombrado el proyecto: `telegram_qr_validator` → `telegram_bot_operativos`.
- `Settings.db_path` → `Settings.database_url` (variable de entorno `DB_PATH` → `DATABASE_URL`). Por defecto sigue siendo SQLite (`sqlite:///run/watches.sqlite3`): no es un cambio disruptivo para quien ya corría el bot sin Docker, solo cambia el nombre de la variable.

### Documentación
- `README.md` y `CONTRIBUTING.md` reescritos: arquitectura, flujo completo, variables de entorno, puesta en marcha con Docker (recomendada) y sin Docker, despliegue a producción en VPS, operación habitual, convenciones de tipado y calidad.

## [1.0.0] - 2026-11-19

Primera versión documentada (bajo el nombre `telegram_qr_validator`): validación de QR de abordaje vía Mini App de Telegram, consulta de solo lectura a Odoo, vigilancia de tickets con aviso al validador y al pasajero (vía `telegram_boarding_bot`), estado persistido en SQLite. 66 pruebas.
