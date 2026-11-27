# Combustible: Mini App de Telegram → BD local → Odoo (`fuel` por XML-RPC, gastos por REST)

Módulo `app/combustible/` de `telegram_bot_operativos`. El conductor registra cada tanqueo del vehículo
reservado a su nombre desde la Mini App **⛽ Combustible** (cuarto botón del panel 📋 Formularios, debajo de
🗺️ Prueba de Ruta) o con `/combustible`. Sigue el mismo patrón del preoperacional, el AST y la Prueba de Ruta:
BD local primero, cola hacia Odoo, `initData` firmado y envío sin señal.

Referencia funcional: `docs/mapa_viacotur_form.md` § 3.5. Infraestructura compartida: `docs/PREOPERACIONAL.md`.

---

## 1. Decisiones (2026-11-25)

| Tema | Decisión |
|---|---|
| Ubicación | Mini App en @OperativosViacoturBot, debajo de 🗺️ Prueba de Ruta; comando `/combustible`. |
| Vehículo y contrato | El vehículo es el **reservado por el preoperacional de la jornada** (`reservas_vehiculo`) y el contrato (`agreement_id`) es el de ese preoperacional, que salió de su extracto aprobado. Sin reserva: "Primero el Preoperacional" (409, `X-Error-Code: sin_reserva_vigente`). |
| Reserva vigente (regla 2026-11-26) | **Reserva vigente (regla 2026-11-26).** Exige una reserva vigente de vehículo, es decir, un preoperacional registrado en la jornada: existe en `reservas_vehiculo` para el id de Telegram, no se liberó con 🔴 Finalizar y no venció (`RESERVA_HORAS` = 24 h). **No** exige que la fecha sea hoy (un turno nocturno hace su Pos-Operacional pasada la medianoche). Sin ella el `context` y el `POST` responden **409** con `X-Error-Code: sin_reserva_vigente` y «Para usar este formulario primero debes registrar el ✅ Preoperacional de hoy (vehículo reservado).»; la Mini App muestra la pantalla de bloqueo **Primero el Preoperacional** con «Volver al chat». Helper único: `reservas.reserva_vigente` (y `preop_service.exigir_reserva`). Envío sin señal: se acepta si la reserva estaba vigente **al abrir** el formulario (`apertura`, firmada por Telegram) según `reservas_historial` (`reservas.reserva_al_instante`), aunque ya se haya liberado al llegar; si no, 409. |
| Pestañas | **Combustible** → modelo `fuel` (XML-RPC). **Combustible para gastos** → `hr.expense` por la API REST (`POST /api/gastos/register`), ya no al modelo `fuel` (2026-11-26). |
| Referencia de la factura | **Se quitó** de ambas pestañas: la Mini App no la muestra ni la envía y `fuel` ya no recibe `ref_invoice`. El esquema aún acepta `ref_factura` (reenvíos sin señal de clientes viejos) pero se ignora. |
| Observaciones | Campo nuevo, **solo en gastos** (opcional, máx. 500): va en `description` del gasto. Vacío → se envía `""` (la clave siempre va). Se guarda en `combustible.observaciones`. |
| Proveedor | En gastos el proveedor se sigue pidiendo y guardando en la BD local, pero **no se envía** (el endpoint no lo recibe). Lista local de **proveedores aprobados** (`proveedores_combustible`, se carga desde un CSV). Si el proveedor no está, el conductor **lo escribe** y va a Odoo como texto en `supplier`. Uno de la lista con contacto de Odoo va en `proveedor` (`res.partner`); sin contacto, su nombre va en `supplier`. |
| Factura | **Solo PDF** (el botón «Elegir PDF» no usa cámara ni «Abrir App para escanear»: ya era solo PDF) (firma `%PDF-`, máx. 4 MB). **Obligatoria solo en "Combustible para gastos"**; opcional en "Combustible". |
| Kilometraje | Tiene que superar al último registrado (se valida en la Mini App y en el servidor, con la fuente de la pestaña del envío). **Fuentes:** (1) odómetro en vivo de Odoo o flota sincronizada y (2) odómetro del preoperacional de la jornada: el mismo km **vale** (mayor o igual); (3) mayor `kilometraje` de los tanqueos **locales** del mismo vehículo (ambas pestañas; no cuentan los `fallido`); (4) mayor `km_tanking` de `fuel` en Odoo para el vehículo; (5) **no se usa**: el mayor `km_tanking` de los `hr.expense` de combustible del empleado (`OdooWriter.ultimo_km_gastos`, parámetro `km_gastos`) existe pero **no se cablea** en `app/main.py`: en qa2 (2026-11-26) Odoo comparó el km del gasto contra el `fuel` del vehículo (12600), no contra los gastos del empleado (173700), y `hr.expense` no tiene vehículo, así que por empleado bloquearía al conductor que cambia de vehículo. Las fuentes 3 y 4 son de tanqueos previos y exigen un km **estrictamente mayor** (Odoo rechaza el igual); con empate manda la regla estricta. Las lecturas de Odoo son tolerantes: si fallan se deja un `WARNING` y se valida con las demás fuentes, sin bloquear al conductor. El reenvío del mismo uuid no se compara contra sí mismo. |
| Total | `amount` (fuel) / `total_amount` (gastos) = galones × precio por galón (se muestra en vivo en la Mini App). |
| Fecha | Por defecto hoy (hora de Colombia); no futura y como máximo 30 días atrás. |
| Ubicación | **Obligatoria** en ambas pestañas, pedida **en el chat** (no en la Mini App) cuando el servidor acepta el registro (también un reenvío sin señal). El bot envía "Registro recibido… envía tu ubicación" con un teclado de un botón 📍 (`request_location`). El registro queda **retenido**: no sale a Odoo hasta tener ubicación. Recordatorios a los **10 y 30 min** del envío (máximo 2); después sigue retenido sin límite (visible en la BD y en el log). |
| Ubicación en gastos | `hr.expense` **no tiene** `lat_origin`/`long_origin` (qa2: 400 "Invalid field 'lat_origin'"). En gastos la ubicación viaja como texto en dos campos char nuevos: `ubicacion_gps_telegram` (la compartida en Telegram) y `ubicacion_gps_vehiculo` (posición del GPS de la flota, fecha y distancia). La pestaña Combustible (`fuel`) no cambia: sigue con `lat_origin`/`long_origin`. |
| Cotejo con el GPS | Solo **calcular y registrar** la distancia: no se rechaza ni se avisa a nadie. Si el GPS falla, el texto es `GPS no disponible` y el gasto sale igual (el GPS nunca retiene). Ver § 2.3. |
| Sin señal | Regla de todos los formularios (`docs/GUIA_NUEVOS_BOTS.md` § 5), incluida la factura. |
| Aviso | El bot confirma al conductor en el chat cuando el registro llega a Odoo. No hay aviso al jefe (el modelo no tiene plan de acción). |

## 2. Modelo `fuel` de Odoo (`fields_get` en qa2, 2026-11-25)

"Tabla para almacenar el combustible" (módulos `calculate_fuel_efficiency`, `gps_logs`, `hr_expeses_inherit`,
`maintenance_request_inherit`, `preoperacional`). El usuario técnico puede leer y crear.

| Campo | Tipo | Lo envía el bot |
|---|---|---|
| `employee_id` | many2one `hr.employee` ("Comprador") | Empleado del PIN |
| `vehicle_id` | many2one `fleet.vehicle` | Vehículo reservado por el preoperacional |
| `agreement_id` | many2one `agreement` ("Contrato") | Contrato del preoperacional (si lo hay) |
| `company_id` | many2one `res.company` | Compañía del empleado |
| `date` | date | Fecha de la transacción |
| `is_fuel_expenses` | boolean | `true` en "Combustible para gastos" |
| `proveedor` | many2one `res.partner` | Proveedor de la lista con contacto de Odoo |
| `supplier` | char ("Estación de Servicios") | Proveedor escrito a mano, o de la lista sin contacto |
| `gallon` | float | Galones |
| `price_gallon` | monetary | Precio por galón |
| `amount` | monetary ("Precio Total") | galones × precio |
| `fuel_type` | selection (**obligatorio**) | `gasoline`, `extra`, `diesel` (por defecto), `electric`, `gas`, `glp`, `diesel urea`, `hydrogen` |
| `km_tanking` | float | Kilometraje al tanqueo |
| `image_bill` | binary ("Factura Tanqueo") | PDF en base64 |
| `attachment_filename`, `fuel_attachment` | char, boolean | Nombre del PDF y `true` cuando se adjunta |
| `name`, `currency_id` | — | Los asigna Odoo (`name` es el nombre del vehículo; no es un consecutivo) |
| `ref_invoice` | char | Ya no se envía (se quitó de la Mini App) |
| `lat_origin`, `long_origin` | sin verificar (float) | Ubicación compartida en el chat (solo si existe) |
| `buyer`, `cash_tannage` | — | No se envían |

## 2.1 Gastos: `hr.expense` por REST (`POST /api/gastos/register`)

Cliente `OdooRestClient` (`app/preop/rest_client.py`): mismo login JWT (`ODOO_API_LOGIN`/`ODOO_API_PASSWORD`) y
base `ODOO_API_URL` (o `ODOO_URL`). Cuerpo exacto:

```json
{"name": "COMBUSTIBLE", "product_id": 9629, "total_amount": 126200.0, "employee_id": 1,
 "description": "Observaciones del conductor", "date": "2026-11-03", "is_fuel_expenses": true,
 "gallon": 12.222, "fuel_type": "diesel", "km_tanking": 173700.0, "type_file": "pdf",
 "attachment_filename": "factura.pdf", "attachment": "<base64 del PDF>"}
```

| Clave | Origen |
|---|---|
| `name`, `is_fuel_expenses`, `type_file` | Constantes: `"COMBUSTIBLE"`, `true`, `"pdf"` (el adjunto es solo PDF) |
| `product_id` | `COMBUSTIBLE_GASTOS_PRODUCT_ID` del `.env` (`product.product`): **9629 en producción** (valor por defecto), **77 en qa2** (`[I011] COMBUSTIBLE`). Con un id que no existe en el ambiente, Odoo responde 500 *Record does not exist… product.product(…)* |
| `total_amount` | galones × precio por galón |
| `employee_id` | id Odoo del empleado |
| `description` | Observaciones (`""` si va vacía) |
| `date`, `gallon`, `fuel_type`, `km_tanking` | Los mismos valores del formulario |
| `ubicacion_gps_telegram` | Texto `"6.167371,-75.641203"` (lat,lon con 6 decimales, de la ubicación compartida en Telegram) |
| `ubicacion_gps_vehiculo` | Texto `"6.167500,-75.641000 · 2026-11-26 09:04 · 18 m"` (posición GPS del vehículo, fecha/hora de esa posición en hora de Colombia y distancia en metros contra la de Telegram) o `"GPS no disponible"` |

Las dos claves de ubicación se omiten solo en filas viejas sin ubicación. **No** se envían `lat_origin`/`long_origin` a este endpoint.
| `attachment_filename`, `attachment` | La factura PDF (obligatoria) en base64 |

No viajan proveedor, precio por galón, vehículo ni contrato (solo quedan en la BD local).

- **Idempotencia:** el endpoint crea un registro, así que antes de **cada** POST se busca por XML-RPC (solo lectura,
  `OdooWriter.buscar_gasto_combustible`) un `hr.expense` con `employee_id` + `is_fuel_expenses` + `km_tanking` + `date` +
  `total_amount` iguales; si existe se toma su id y no se postea. Si esa búsqueda falla de forma permanente (p. ej. el
  campo no existe en `hr.expense`) se deja un `WARNING` en el log y se continúa con el POST; si falla por red se
  reintenta después sin postear. El POST usa `post_json_once` (sin el reintento automático de `post_json`): el
  reintento queda en la cola, siempre precedido de la verificación.
- **Errores:** 4xx → `fallido` (definitivo, reencolable con `--requeue`); 5xx/429/red → `error` con backoff, y `fallido`
  al agotar `PREOP_MAX_ATTEMPTS`.
- **Id devuelto:** la respuesta no está documentada; se busca `id`, `expense_id`, `gasto_id`, `record_id` (también
  dentro de `result`/`data`/`response`) y se guarda en `combustible.odoo_gasto_id` (columna aparte de `odoo_id`, que es
  única y de `fuel`: los ids de dos modelos podrían chocar). Si no viene, el registro se marca enviado igualmente y la
  respuesta (hasta 500 caracteres) queda en el log.
- **Sin credenciales REST:** si `ODOO_API_LOGIN`/`ODOO_API_PASSWORD` no están, los gastos quedan en `error` con un
  `ERROR` claro en el log y **sin consumir intentos** (se reintentan cada ~10 min); no se pierden.

**Comportamiento verificado en qa2 (2026-11-26, `hr.expense` 161):** `/api/gastos/register` puede **crear el gasto y aun así
responder 500** (p. ej. "Kilometraje inválido: 12560.0 (último) <= 12600.0 (penúltimo)"). Por eso la búsqueda previa
anti-duplicados lo recupera en el reintento y no se crea otro: ese flujo es correcto y no cambia. Pendiente para el
equipo de Odoo: validar el kilometraje **antes** de crear y responder 4xx.

## 2.2 Ubicación del tanqueo (flujo)

1. `POST /api/combustible` → 202; si el registro es nuevo, `CombustibleServices.on_accepted` (en `app/main.py`) manda el
   mensaje con el botón 📍. El registro se guarda con `ubicacion_requerida = true`.
2. `claim_batch` solo reclama filas con `ubicacion_requerida` no verdadera **o** con `latitud` (retención).
   Las filas anteriores al despliegue (columna NULL) siguen saliendo como antes.
3. `on_location` (`app/telegram_app.py`) reparte la ubicación entre **Combustible y Gastos Operativos**: va al pendiente **más antiguo** de los dos (`gastos_operativos.service.asignar_ubicacion_mas_antigua`; con empate, Combustible) y el siguiente se vuelve a pedir (`docs/GASTOS_OPERATIVOS.md` § 5). Para Combustible conserva la asociación al preoperacional y, además, asigna la ubicación al
   tanqueo **más antiguo** del usuario que la espera (`service.asignar_ubicacion`). Se ignoran los mensajes
   reenviados y las ubicaciones con fecha anterior a la recepción del registro; las ubicaciones en vivo editadas siguen
   ignoradas. Confirma con la placa y, si quedan más pendientes, vuelve a pedir la ubicación.
4. `CombustibleSender.run_once` envía los recordatorios (10 y 30 min; `RECORDATORIOS_UBICACION_MIN`) y marca cada uno
   antes de enviarlo (a lo sumo una vez; un fallo de Telegram se registra y no frena la cola).
5. Con la ubicación, el registro se envía: **Combustible** a `fuel` con `lat_origin` / `long_origin` (floats);
   **gastos** a `hr.expense` con `ubicacion_gps_telegram` y `ubicacion_gps_vehiculo` (texto, § 2.3).
6. `on_location` despierta la cola de combustible (`COMBUSTIBLE_WAKE_KEY` en `bot_data`, cableado en `app/main.py`):
   el envío —y la consulta al GPS— ocurre segundos después, sin esperar al siguiente ciclo.

Columnas nuevas en `combustible` (se añaden solas): `latitud`, `longitud`, `ubicacion_at`, `ubicacion_requerida`,
`recordatorios_ubicacion`. Consulta de retenidos:
`SELECT id, telegram_user_id, fecha, recordatorios_ubicacion FROM combustible WHERE ubicacion_requerida AND latitud IS NULL;`

## 2.3 Cotejo con el GPS de la flota

Módulo `app/combustible/gps.py` (cliente `httpx`, inyectable en `CombustibleSender`/`send_one`).

| Tema | Detalle |
|---|---|
| Endpoint | `GET http://31.97.98.223:8081/api/v1/external/flota` (solo lectura) |
| Autenticación | Cabecera `x-api-key`. La clave nunca se escribe en logs (en los fallos solo se registra el tipo de error) |
| Variables `.env` | `GPS_FLOTA_URL` (vacío = GPS desactivado), `GPS_FLOTA_API_KEY` (secreta), `GPS_FLOTA_TIMEOUT_SECONDS` (10). Sin URL o sin clave: `WARNING` una vez al arrancar y todos los gastos van con `GPS no disponible` |
| Respuesta | La envoltura no está confirmada: se acepta una lista en la raíz o dentro de un dict (`data`, `items`, `vehiculos`, `flota`, `result`, `results`). Elemento: `{"vehiculo_id", "timestamp": "2026-11-26T09:26:44-05:00", "coordenadas": {"lat", "long"}, "matricula": "Agrale/Ma 8.7 Euro V/EQX557", "velocidad"}` |
| Identificación | Por **placa**: último segmento de `matricula` tras `/` (`EQX557`), comparado normalizado (mayúsculas, sin espacios ni guiones) con la placa del vehículo reservado del registro (`reservas.placa`). Varias coincidencias: la de timestamp más reciente |
| Distancia | Haversine (radio 6 371 km) entre la ubicación de Telegram y la del GPS, redondeada a metros enteros |
| Momento | Al enviar el gasto (`send_one`, antes de armar el payload), solo si el registro aún no tiene la instantánea. Se guarda la primera vez (incluido `GPS no disponible`) y **no se vuelve a consultar** en reintentos |
| Fallos | Sin configurar, timeout/red, HTTP distinto de 200, JSON ilegible, vehículo ausente, coordenadas o fecha inválidas -> `GPS no disponible`; el gasto se envía igual |

Columnas nuevas de `combustible` (se añaden solas con `Database._NEW_COLUMNS`):

| Columna | Contenido |
|---|---|
| `gps_latitud`, `gps_longitud` | Posición del vehículo según el GPS (NULL si no hubo dato) |
| `gps_at` | Fecha y hora de esa posición (UTC) |
| `gps_distancia_m` | Distancia en metros a la ubicación de Telegram |
| `ubicacion_gps_vehiculo` | Texto final enviado a Odoo; NULL = aún no consultada |

## 3. Arquitectura y archivos

```
Conductor ──/combustible o ⛽──▶ Mini App /combustible (app/static/combustible.html)
Mini App ──initData (HMAC)──▶ POST /api/combustible ──▶ combustible + combustible_facturas (pendiente) ──▶ 202
CombustibleSender ─┬─ pestaña Combustible ──▶ OdooWriter.create_combustible ──▶ Odoo `fuel`
                   └─ pestaña gastos ──▶ búsqueda previa (XML-RPC) + POST /api/gastos/register ──▶ `hr.expense`
                   └──▶ aviso al conductor
```

| Archivo | Contenido |
|---|---|
| `app/combustible/catalogo.py` | Fuente única: tipos de combustible (selection de Odoo), pestañas, rango de fecha. |
| `app/combustible/proveedores.py` | Lista de proveedores/estaciones aprobadas: lectura del CSV (con ubicación, errores por fila), vista previa, carga (UPSERT) y CLI. La carga con vista previa también se hace desde el Panel Admin (`docs/PANEL_ADMIN.md`). |
| `app/combustible/schemas.py` | `CombustibleIn`: decimales con coma → punto, proveedor (lista o texto, exactamente uno), factura PDF por firma real y obligatoria en gastos, fecha. |
| `app/combustible/service.py` | Contexto, `submit` / `submit_diferido`, kilometraje mínimo, `build_odoo_values`, `build_gasto_payload`, `enviar_gasto`, `send_one`. |
| `app/combustible/router.py` · `worker.py` | Rutas y cola (`CombustibleSender`, CLI). |
| `app/preop/models.py` | Tablas `proveedores_combustible`, `combustible`, `combustible_facturas`, `combustible_envios_diferidos`. |
| `app/combustible/gps.py` | Cliente del GPS de la flota, parseo tolerante, haversine y formatos de texto. |
| `app/preop/rest_client.py` | `post_json_once`: POST sin reintento automático (gastos). |
| `app/preop/odoo_writer.py` | `buscar_gasto_combustible` (solo lectura), `create_combustible` (external id `combustible_<uuid>`; llave natural `employee_id` + `vehicle_id` + `km_tanking` en los últimos 2 min) y `buscar_contacto` (solo lectura, para la carga de proveedores). |

### Tablas

- **`proveedores_combustible`:** `nombre`, `clave` (nombre normalizado, única), `nit` (solo dígitos, único),
  `odoo_partner_id`, `activo` y, opcionales, `latitud`, `longitud`, `ciudad` y `direccion` (estación con
  ubicación; las añade `Database._NEW_COLUMNS` a las bases existentes).
- **`combustible`:** `uuid` (único), identidad de Telegram, `empleado_id`, `vehiculo_id`, `preop_id`,
  `contrato_odoo_id`, `fecha_transaccion`, `para_gastos`, proveedor (`proveedor_id`, `proveedor_nombre`,
  `proveedor_odoo_id`), `galones`, `precio_galon`, `total`, `tipo_combustible`, `kilometraje`, `ref_factura` (en
  desuso), `observaciones` (gastos), `odoo_id` (`fuel`), `odoo_gasto_id` (`hr.expense`) y la
  auditoría de sincronización común.
- **`combustible_facturas`:** el PDF en `BYTEA` con `nombre_archivo`, `tamano` y `sha256` (fuera de la tabla
  principal para no cargar binarios al leer la cola).
- **`combustible_envios_diferidos`:** trazabilidad sin señal (`aceptado` / `vencido`); un vencido guarda el
  formulario y la factura.

## 4. Mini App (`/combustible`)

Dos pasos: **Registro → Resumen**.

1. **Registro:**
   - *Información general:* comprador, contrato y vehículo reservado (solo lectura).
   - *Proveedor:* pestañas Combustible / Combustible para gastos; buscador "Buscar o escribir proveedor…" que
     filtra la lista de aprobados (sin tildes ni mayúsculas) y ofrece **"✎ Usar «…» (no está en la lista)"**;
     fecha de la transacción. **Cercanía (2026-11-26):** si el teléfono da permiso, `navigator.geolocation` ordena
     la lista de la más cercana a la más lejana y cada estación muestra «a X km» (haversine en el teléfono);
     las estaciones sin ubicación quedan al final. La ubicación es **opcional y no bloquea**: sin permiso o sin
     señal se ordena por nombre. Se sigue pudiendo escribir un proveedor a mano. No cambia el envío sin señal.
   - *Detalles:* galones y precio por galón (coma → punto al escribir), **total en vivo** y tipo de combustible
     (Diesel / ACPM por defecto).
   - *Referencias:* kilometraje (solo dígitos, con el último registrado como guía), **Observaciones** (solo en gastos) y
     **factura PDF** (obligatoria en "para gastos").
2. **Resumen:** todos los datos y el total. **Enviar registro** pide confirmación con el valor.

"Revisar →" nunca queda gris sin explicación: si falta algo, lista todo lo pendiente y cada elemento lleva a su
campo. Pantallas de bloqueo: **Primero el Preoperacional** y usuario no registrado.

## 5. Rutas

| Ruta | Respuesta |
|---|---|
| `GET /combustible` | HTML (sin caché; logo versionado) |
| `GET /api/combustible/form` | tipos, pestañas, rango de fecha y **proveedores aprobados activos** (`id`, `nombre`, `latitud`, `longitud`, `ciudad`, `direccion`; la ubicación puede ser `null`) |
| `GET /api/combustible/context` | `empleado`, `contrato`, `vehiculo` reservado (**409** sin reserva vigente), `ultimo_km` / `ultimo_km_gastos` (con `*_estricto`: debe ser mayor, no igual; la Mini App usa el de la pestaña activa) y `hoy`; 403 sin PIN |
| `POST /api/combustible` | **202**; 401 firma, 403 sin empleado, 409 sin reserva vigente (`X-Error-Code: sin_reserva_vigente`) / vehículo distinto / uuid ajeno, 422 validación o kilometraje menor, **410** reenvío sin señal vencido |
| `GET /api/combustible/{uuid}` | estado de un envío propio |

## 6. Operación

| Tarea | Comando |
|---|---|
| Cargar proveedores aprobados | Copiar el CSV al contenedor y `docker compose exec app python -m app.combustible.proveedores cargar /tmp/proveedores.csv --resolver-odoo` (ver plantilla `docs/proveedores_combustible_ejemplo.csv`) |
| Reemplazar la lista completa | Igual, con `--reemplazar` (desactiva los que no vengan en el archivo) |
| Ver la lista | `docker compose exec app python -m app.combustible.proveedores listar` |
| Quitar un proveedor | `docker compose exec app python -m app.combustible.proveedores desactivar ID` |
| Cotejo GPS | `SELECT id, ubicacion_gps_vehiculo, gps_distancia_m, gps_at FROM combustible WHERE para_gastos ORDER BY id DESC LIMIT 20;` (`GPS no disponible` = ver el `WARNING` del log) |
| Retenidos sin ubicación | `SELECT id, telegram_user_id, fecha, recordatorios_ubicacion FROM combustible WHERE ubicacion_requerida AND latitud IS NULL;` |
| Reenviar uno fallido | `docker compose exec app python -m app.combustible.worker --requeue ID` y luego `--once` |
| Cola | `SELECT sync_status, count(*) FROM combustible GROUP BY 1;` |
| Envíos sin señal | `SELECT estado, uuid, sin_senal_desde, recibido_at, motivo FROM combustible_envios_diferidos ORDER BY recibido_at DESC;` |
| Procesar la cola | `docker compose exec app python -m app.combustible.worker --once` |
| Reencolar fallidos | `docker compose exec app python -m app.combustible.worker --requeue-all` |

El CSV lleva encabezado `nombre;nit;odoo_partner_id;latitud;longitud;ciudad;direccion` (separado por `;` o `,`,
UTF-8). Solo `nombre` es obligatorio; `latitud` y `longitud` van juntas (grados decimales en rango; con `;` se
acepta también la coma decimal). Un CSV sin ubicación no borra la ya guardada. **Estaciones con ubicación y carga
con vista previa desde el Telegram de un administrador:** Panel Admin, pestaña ⛽ Estaciones (`docs/PANEL_ADMIN.md`). Se actualiza por NIT o, sin NIT, por nombre normalizado. `--resolver-odoo` busca en Odoo (solo lectura)
el contacto de las filas sin `odoo_partner_id`, por NIT y luego por nombre exacto, y solo lo asigna si es único;
al final lista los proveedores que quedaron sin contacto (irán como texto en `supplier`).

## 7. Pendientes

- [ ] Cargar la lista real de proveedores aprobados (hoy la tabla está vacía: el conductor puede escribirlos).
- [ ] Confirmar el formato de la respuesta de `/api/gastos/register` (hoy el id se extrae de forma tolerante) y hacer la prueba real en qa2 (crea un `hr.expense`); verificar también si `hr.expense` tiene `km_tanking` para la búsqueda anti-duplicados.
- [ ] Prueba de punta a punta en qa2 y desde un móvil (crea un registro real en `fuel`).
- [ ] Confirmar si el kilometraje del tanqueo debe actualizar el odómetro del vehículo en Odoo (hoy no se escribe en `fleet.vehicle.odometer`).
- [ ] Verificar en qa2 el tipo real de `lat_origin` / `long_origin` en `fuel` (hoy se envían como float).
- [ ] Confirmar la envoltura real de la respuesta del GPS de flota y fijar `GPS_FLOTA_URL`/`GPS_FLOTA_API_KEY` en el `.env` de qa2.
- [ ] Reenviar el registro de prueba id 2 (falló con 400 por `lat_origin`): `python -m app.combustible.worker --requeue 2` y luego `--once`.
- [ ] Equipo de Odoo: que `/api/gastos/register` valide el kilometraje antes de crear y responda 4xx (hoy crea y responde 500).
- [ ] Confirmar que `hr.expense` ya tiene `ubicacion_gps_telegram` y `ubicacion_gps_vehiculo`.
