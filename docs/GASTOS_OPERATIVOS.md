# Gastos Operativos: Mini App de Telegram → BD local → `hr.expense` de Odoo (REST)

Módulo `app/gastos_operativos/` de `telegram_bot_operativos`. El conductor registra los gastos de su jornada
(alimentación, hospedaje, peajes…) desde la Mini App **💰 Gastos Operativos** (quinto botón del panel 📋 Formularios,
**justo debajo de ⛽ Combustible**) o con `/gastos`. Sigue el mismo patrón de paquete que `app/combustible/`
(schemas, service, router, worker y CLI, cola local, `initData` firmado, envío sin señal y ubicación obligatoria) y
reutiliza sus piezas: `enviar_gasto`, `tomar_instantanea_gps`, `asignar_ubicacion` y `recordatorios_pendientes`.

Referencia funcional: `docs/mapa_viacotur_form.md` §§ 2 y 3.6. Pieza hermana: `docs/COMBUSTIBLE.md` (§ 2.1 gastos por
REST, § 2.2 ubicación, § 2.3 GPS).

---

## 1. Flujo

```
Conductor ──💰 / /gastos──▶ Mini App /gastos-operativos (app/static/gastos_operativos.html)
Mini App ──initData (HMAC)──▶ POST /api/gastos-operativos ──▶ gastos_operativos + líneas + soportes PDF (pendiente, retenido) ──▶ 202
Bot ──▶ "N gastos recibidos" + instrucciones + botón 📍 Enviar ubicación
Conductor ──📍──▶ on_location ──▶ la ubicación se guarda en el envío (ya no está retenido) y despierta la cola
GastosOperativosSender ──▶ cotejo con el GPS de la flota (una vez) ──▶ por cada línea: búsqueda previa (XML-RPC)
                           + POST /api/gastos/register ──▶ un hr.expense por gasto ──▶ aviso final al conductor
```

1. **Formulario.** Conductor (solo lectura), **fecha del gasto** (hoy en hora de Colombia; no futura y máximo 30 días
   atrás) y el botón **+ Agregar gasto**, que agrega tarjetas «Gasto #N» con botón 🗑 de eliminar (hasta 10 por envío).
   Cada tarjeta lleva: **tipo de gasto** (select, obligatorio), **valor total en COP** (entero mayor que 0, se muestra
   `$ 65 000` en vivo), **soporte** obligatorio (📷 tomar foto, 🖼️ galería o PDF) y **observación** opcional (máx. 500).
   Una tarjeta **Total general** suma todos los valores y una barra fija ofrece **Cancelar** y **Enviar**. Si falta algo,
   el aviso de pendientes lista todo y cada elemento lleva a su campo (nunca un botón gris sin explicación).
2. **Sin vehículo reservado** no se bloquea: aparece el aviso amarillo «El gasto se registrará sin cotejo con el GPS del
   vehículo». Con vehículo se muestra su placa.
3. **Recepción.** El servidor valida (esquema + tipos + soportes), convierte las fotos a PDF y guarda el envío como
   `pendiente` y **retenido**. Un reenvío con el mismo `uuid` responde como duplicado y no repite el aviso.
4. **Ubicación.** El bot responde «💰 N gastos recibidos» con las instrucciones de `docs/mapa_viacotur_form.md` § 2
   (1️⃣ ya enviaste el formulario, 2️⃣ toca 📍 Enviar ubicación, ⏱️ tienes 10 minutos) y el teclado con el botón 📍.
5. **Envío a Odoo.** Con la ubicación, el worker envía **un `hr.expense` por cada tarjeta**. Al terminar avisa al
   conductor: «N gastos registrados en Odoo» o el detalle de los que no se pudieron guardar.

## 2. Payload a Odoo (`POST /api/gastos/register`, uno por tarjeta)

```json
{"name": "ALIMENTACIÓN", "product_id": 1, "total_amount": 65000.0, "employee_id": 100,
 "description": "Almuerzo en ruta", "date": "2026-11-26", "is_fuel_expenses": false,
 "type_file": "pdf", "attachment_filename": "factura.pdf", "attachment": "<base64 del PDF>",
 "ubicacion_gps_telegram": "6.250000,-75.560000", "ubicacion_gps_vehiculo": "GPS no disponible"}
```

| Clave | Origen |
|---|---|
| `name` | Nombre del tipo en **mayúsculas** |
| `product_id` | El del tipo (`GASTOS_OPERATIVOS_TIPOS`), guardado en la línea al recibir el envío |
| `total_amount` | Valor de la tarjeta (float) |
| `employee_id` | Id Odoo del empleado (PIN = id de Telegram) |
| `description` | Observación (`""` si va vacía; la clave siempre va) |
| `date` | Fecha del gasto del formulario (la misma para todas las tarjetas) |
| `is_fuel_expenses` | Siempre `false` |
| `type_file`, `attachment_filename`, `attachment` | Soporte **siempre PDF** (una foto ya viene convertida) en base64; el nombre es el original sin ruta y con extensión `.pdf` |
| `ubicacion_gps_telegram` | `"lat,lon"` con 6 decimales, de la ubicación compartida en el chat (la misma en todas las líneas) |
| `ubicacion_gps_vehiculo` | Texto con la posición del GPS de la flota, fecha/hora y distancia, o `"GPS no disponible"` |

**No se envían** `gallon`, `fuel_type` ni `km_tanking` (no aplican; se **omiten**, no van vacíos). Tampoco
`lat_origin`/`long_origin` (`hr.expense` no los tiene). Las claves de ubicación se omiten solo si el envío no tiene
ubicación (no ocurre con envíos nuevos: quedan retenidos hasta tenerla).

### Dedupe

Antes de **cada** POST se busca por XML-RPC (solo lectura, `OdooWriter.buscar_gasto_operativo`) un `hr.expense` con
`employee_id` + `date` + `total_amount` + `product_id` + `is_fuel_expenses = false`. Si existe se toma su id y no se
postea. Detalles:

- `excluir_ids`: la búsqueda excluye los `hr.expense` que otras líneas **del mismo envío** ya tienen asignados, para que
  dos tarjetas iguales (mismo tipo y valor, p. ej. dos peajes de $ 8 500) sean **dos** gastos y no colapsen en uno.
- Si la búsqueda falla de forma permanente (p. ej. un campo inexistente) se deja un `WARNING` y se envía igual; si falla
  por red se reintenta después **sin postear**.
- El POST usa `post_json_once` (sin reintento automático): el reintento lo hace la cola, siempre precedido de la búsqueda.
- Limitación conocida: la llave natural no distingue un gasto idéntico de **otro envío** del mismo día (mismo empleado,
  tipo, valor y fecha); en ese caso la línea se toma como ya registrada.

El id devuelto se busca de forma tolerante (`id`, `expense_id`, `gasto_id`, `record_id`, también dentro de
`result`/`data`/`response`) y se guarda en `gastos_operativos_lineas.odoo_gasto_id`.

## 3. Tipos de gasto configurables

Variable `GASTOS_OPERATIVOS_TIPOS`: `Nombre:product_id` separados por `;`.

```
GASTOS_OPERATIVOS_TIPOS=Alimentación:1;Hospedaje:2;Peajes:6;Parqueadero:6;Lavado:6;Otros:6
```

- El `product_id` es el de `product.product` y **cambia por ambiente**. En qa2: Meals = 1, Travel & Accommodation = 2,
  Others = 6; en producción solo cambian los ids (**confirmarlos antes de desplegar**: el valor por defecto del código y
  de `.env.example` es el de qa2).
- El orden del `.env` es el orden del select. El nombre se usa tal cual en la Mini App y, en mayúsculas, como `name`
  del gasto. Dos tipos pueden compartir `product_id` (p. ej. Peajes, Parqueadero y Lavado → 6).
- **Validación al arrancar** (`app/main.py`, `_start_gastos_operativos`): un error de formato impide iniciar el bot con
  un mensaje claro, p. ej. `Configuración inválida: GASTOS_OPERATIVOS_TIPOS: el product_id de «Hospedaje» debe ser un
  número entero mayor que cero (llegó «dos»)`. Se rechaza: valor vacío, parte sin `:` o sin nombre, `product_id` no
  numérico o ≤ 0, nombres repetidos (sin distinguir mayúsculas) y nombres de más de 60 caracteres.
- **Panel Admin:** el parámetro está en la lista blanca (`app/admin/envfile.py`) con el mismo validador y se guarda
  normalizado (`Alimentación:1;Otros:6`). Ver `docs/PANEL_ADMIN.md` § 6. Cambiarlo requiere reiniciar el servicio `app`.
- La Mini App recibe solo los nombres; el servidor resuelve el `product_id` al recibir el envío y lo guarda en cada línea,
  así que cambiar el `.env` después no altera lo ya recibido. Un tipo que ya no existe se rechaza con 422.

## 4. Soportes

- **PDF:** se valida por su firma real (`%PDF`), no por lo que diga el cliente; pasa tal cual.
- **Foto:** JPG o PNG (por firma). El servidor la convierte a un PDF de una página con **Pillow** (`requirements.txt`):
  corrige la orientación EXIF, aplana la transparencia sobre blanco y rechaza fotos corruptas o de más de 60 M de píxeles.
  **HEIC/HEIF no se soporta** en el servidor (no hay librería): la Mini App reduce cada foto a un lado máximo de 1 800 px y
  la vuelve a codificar como JPG antes de enviarla (así también funcionan las fotos HEIC de iPhone, que Safari sí
  decodifica); si el teléfono no logra leerla pide JPG, PNG o PDF.
- **Límites** (alineados con Combustible; el proxy nginx acepta 10 MB por petición y el base64 pesa 4/3): **3 MB por
  soporte**, **6 MB entre todos los soportes** del envío, máximo **10 gastos** por envío. Se validan en la Mini App y en el
  servidor (422).
- Un soporte ilegible responde 422 con el número del gasto («Gasto #2: No se pudo leer la foto del soporte…») y no se
  guarda nada del envío.

## 5. Ubicación obligatoria y GPS

Mismas reglas que Combustible (`docs/COMBUSTIBLE.md` §§ 2.2 y 2.3), con las mismas funciones parametrizadas por tabla:

1. Aceptado el envío (también un reenvío sin señal), el bot pide 📍 en el chat. El envío completo queda **retenido**
   (`ubicacion_requerida` y `latitud` vacía): `claim_batch` no lo toma.
2. **Recordatorios** a los 10 y 30 min (máximo 2; `RECORDATORIOS_UBICACION_MIN`); se marcan antes de enviarse y un fallo
   de Telegram no frena la cola. Después sigue retenido sin límite (visible en la BD y en el log).
3. La ubicación se guarda en la cabecera y **se usa en todas las líneas**. Se ignoran los mensajes reenviados y las
   ubicaciones anteriores a la recepción del envío.
4. **Cotejo con el GPS** (`app/combustible/gps.py`): una sola consulta por envío, con la **placa del vehículo reservado**.
   Sin vehículo reservado, o con cualquier fallo del GPS, el texto es `GPS no disponible` y el envío sale igual (el GPS
   nunca retiene).
5. **Convivencia con Combustible.** `on_location` llama a `asignar_ubicacion_mas_antigua`
   (`app/gastos_operativos/service.py`): la ubicación va al **pendiente más antiguo entre los dos módulos** (por fecha de
   recepción; con empate gana Combustible), solo cuenta lo recibido antes de la ubicación, y el mensaje de respuesta avisa
   de cuántos registros siguen sin ubicación sumando ambos módulos y vuelve a pedirla. Despierta la cola del módulo que
   la recibió (`GASTOS_WAKE_KEY` o `COMBUSTIBLE_WAKE_KEY`). La ubicación sigue asociándose también al preoperacional reciente.

## 6. Estados

| Nivel | Estados | Notas |
|---|---|---|
| Envío (`gastos_operativos.sync_status`) | `pendiente` → `enviando` → `enviado` / `error` / `fallido` | Unidad de la cola (`claim_batch`, lease, backoff, `PREOP_MAX_ATTEMPTS`) |
| Línea (`gastos_operativos_lineas.sync_status`) | `pendiente` → `enviado` / `error` / `fallido` | Cada línea guarda su resultado apenas lo tiene: un corte a mitad no pierde los ids creados |

- **4xx** de Odoo → la línea queda `fallido` (definitivo, con el motivo en `error_log`); las demás líneas siguen.
- **5xx, 429 o error de red** → la línea queda en `error` y el envío se reintenta con backoff (solo las líneas
  pendientes); al agotar los intentos pasan a `fallido`.
- El envío termina `enviado` si todas las líneas lo están y `fallido` si alguna quedó fallida (parcial).
- **Sin credenciales REST** (`ODOO_API_LOGIN`/`ODOO_API_PASSWORD`): el envío queda en `error` con un `ERROR` en el log y
  **sin consumir intentos** (se reintenta cada ~10 min); no se pierde.
- **Mensajes al conductor:** confirmación al recibir («N gastos recibidos»), petición de ubicación con instrucciones,
  recordatorios, y resultado final: «💰 N gastos registrados en Odoo» (con el total) o «Algunos gastos no pudieron
  guardarse en Odoo» con la lista (`Gasto #N (tipo, valor)`) y la referencia local.

## 7. Envío sin señal

Regla de todos los formularios (`docs/GUIA_NUEVOS_BOTS.md` § 5): bandeja IndexedDB propia `gastos-operativos-offline`
(respaldo `localStorage`) con los soportes incluidos, reenvío cada 15 s y al evento `online`, ventana de **1 h** desde la
pérdida de señal. El cuerpo acepta `diferido: {init_data_original, sin_senal_desde}`. Dentro de la ventana: flujo normal
(incluida la petición de ubicación) + fila `aceptado` en `gastos_operativos_envios_diferidos`. Fuera: **410**, fila
`vencido` con el formulario completo (soportes en base64 dentro de `payload`), sin Odoo ni avisos.

## 8. Rutas

| Ruta | Respuesta |
|---|---|
| `GET /gastos-operativos` | HTML (sin caché; logo versionado) |
| `GET /api/gastos-operativos/form` | tipos (solo nombres), límites y rango de fecha |
| `GET /api/gastos-operativos/context` | conductor, vehículo reservado (o `null`) y `hoy`; 403 sin PIN |
| `POST /api/gastos-operativos` | **202**; 401 firma, 403 sin empleado, 409 uuid ajeno, 422 validación (tipo, valor, soporte, fecha), **410** reenvío sin señal vencido |
| `GET /api/gastos-operativos/{uuid}` | estado de un envío propio |

## 9. Tablas (las crea `create_all`; no se tocan tablas existentes)

- **`gastos_operativos`:** cabecera: `uuid` (único), identidad de Telegram, `empleado_id`, `vehiculo_id` (opcional),
  `fecha` (recepción), `fecha_gasto`, `total`, ubicación (`latitud`, `longitud`, `ubicacion_at`, `ubicacion_requerida`,
  `recordatorios_ubicacion`), GPS (`gps_latitud`, `gps_longitud`, `gps_at`, `gps_distancia_m`, `ubicacion_gps_vehiculo`) y
  la auditoría de sincronización común.
- **`gastos_operativos_lineas`:** una por tarjeta: `numero`, `tipo_nombre`, `product_id`, `valor`, `observacion`, estado,
  `odoo_gasto_id`, `error_log`, `sent_at`.
- **`gastos_operativos_soportes`:** el PDF en `BYTEA` (`nombre_archivo`, `tamano`, `sha256`, `convertido` = venía como foto).
- **`gastos_operativos_envios_diferidos`:** trazabilidad sin señal (`aceptado` / `vencido`).

## 10. Operación

| Tarea | Comando |
|---|---|
| Procesar la cola | `docker compose exec app python -m app.gastos_operativos.worker --once` |
| Reenviar uno fallido | `docker compose exec app python -m app.gastos_operativos.worker --requeue ID` y luego `--once` (solo se reenvían las líneas fallidas; las enviadas no se tocan) |
| Reencolar todos los fallidos | `docker compose exec app python -m app.gastos_operativos.worker --requeue-all` |
| Cola | `SELECT sync_status, count(*) FROM gastos_operativos GROUP BY 1;` |
| Retenidos sin ubicación | `SELECT id, telegram_user_id, fecha, recordatorios_ubicacion FROM gastos_operativos WHERE ubicacion_requerida AND latitud IS NULL;` |
| Líneas de un envío | `SELECT numero, tipo_nombre, valor, sync_status, odoo_gasto_id, error_log FROM gastos_operativos_lineas WHERE gasto_id = ID ORDER BY numero;` |
| Cotejo GPS | `SELECT id, ubicacion_gps_vehiculo, gps_distancia_m, gps_at FROM gastos_operativos ORDER BY id DESC LIMIT 20;` |
| Envíos sin señal | `SELECT estado, uuid, sin_senal_desde, recibido_at, motivo FROM gastos_operativos_envios_diferidos ORDER BY recibido_at DESC;` |

## 11. Archivos

| Archivo | Contenido |
|---|---|
| `app/gastos_operativos/catalogo.py` | `parse_tipos` (formato y errores), límites y `form_definition`. |
| `app/gastos_operativos/soporte.py` | Validación por firma, foto → PDF (Pillow) y nombre del archivo. |
| `app/gastos_operativos/schemas.py` | `GastosOperativosIn` / `GastoIn`: valor entero, soporte, fecha y total de soportes. |
| `app/gastos_operativos/service.py` | Contexto, `submit` / `submit_diferido`, payload, `send_one`, cola, y envoltorios de ubicación/GPS. |
| `app/gastos_operativos/router.py` · `worker.py` | Rutas y cola (`GastosOperativosSender`, CLI). |
| `app/preop/models.py` | Tablas `gastos_operativos*`. |
| `app/preop/odoo_writer.py` | `buscar_gasto_operativo` (solo lectura). |
| `app/combustible/service.py` | Funciones de ubicación y GPS parametrizadas con `model=`; `enviar_gasto(buscar=, etiqueta=)`. |
| `app/static/gastos_operativos.html` | Mini App. |
| `tests/test_gastos_operativos.py` | Pruebas del módulo. |

## 12. Pendientes

- [ ] Confirmar con Odoo que `/api/gastos/register` acepta el payload **sin** `gallon`, `fuel_type` ni `km_tanking`
      (se omiten por no aplicar) y con `is_fuel_expenses: false`.
- [ ] Confirmar que los productos de los tipos están permitidos para el empleado y fijar `GASTOS_OPERATIVOS_TIPOS` con los
      ids de **producción** (el valor por defecto es el de qa2).
- [ ] Confirmar que `hr.expense` ya tiene `ubicacion_gps_telegram` y `ubicacion_gps_vehiculo` (mismos campos que
      «Combustible para gastos») y que la búsqueda por `product_id` e `is_fuel_expenses` es válida en `hr.expense`.
- [ ] Confirmar el formato de la respuesta de `/api/gastos/register` (hoy el id se extrae de forma tolerante).
- [ ] Prueba de punta a punta en qa2 y desde un móvil (crea `hr.expense` reales): foto de cámara y de galería, PDF, varios
      gastos, sin señal y sin vehículo.
- [ ] Soporte HEIC en el servidor: hoy solo lo cubre la reconversión a JPG en la Mini App.
