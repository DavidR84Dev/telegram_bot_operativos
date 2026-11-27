# Correcciones Post y Gastos

Mini App **🛠️ Correcciones Post y Gastos** (`/correcciones`) para que el conductor corrija, desde el chat, **(1)** sus gastos operativos rechazados (`hr.expense`) **(2)** sus Post-operacionales pendientes (`pos.operacional`) y **(3)** cargar la planilla de una jornada que quedó sin Post-operacional. Código en `app/correcciones/`, página en `app/static/correcciones.html`.

## 1. Cómo se abre

- Botón fijo **🛠️ Correcciones Post y Gastos** en el teclado del bot (segunda fila, bajo 📋 Formularios y 📍 Enviar ubicación) y comando `/correcciones`. Ambos responden con un botón **inline** `web_app` que abre la Mini App (desde un botón de teclado no llega `initData`).
- No va en el panel 📋 Formularios (`FORMULARIOS`) y **no exige reserva vigente** del vehículo: la corrección es posterior a la jornada. Es la única excepción a la regla de `tests/test_reserva_requerida.py`.
- Un usuario sin PIN recibe el mismo trato que en los demás formularios: `_authorized` le ofrece 📝 Registro. El PIN del empleado activo es el id de Telegram (`Empleado.codigo_pin`); su `employee_id` de Odoo es `Empleado.odoo_id`.

## 2. Qué se lista (en vivo, solo lectura)

`GET /api/correcciones/context` (cabecera `X-Telegram-Init-Data`) consulta Odoo por XML-RPC (`OdooWriter.gastos_rechazados` / `pos_pendientes`, con lock y sin lanzar excepción: si Odoo falla devuelve `odoo_ok: false` con un mensaje y la Mini App lo muestra):

| Lista | Modelo y filtro | Se muestra |
|---|---|---|
| Gastos rechazados | `hr.expense`, `employee_id` = empleado, `state_expenses` = `refused`, `date` >= hoy − N días | nombre, tipo (`product_id`), valor, fecha, descripción |
| Post-operacionales pendientes | `pos.operacional`, `employee_id` = empleado, `state` en (`refused`, `refused by`, `correct by`), `check_in` >= hoy − N días (o, si `check_in` está vacío, `create_date`) | entrada/salida, vehículo, contrato, km, estado y las anotaciones `corrections` / `corrections_hr` |
| Planillas no enviadas | jornadas del conductor sin Post-operacional (ver § 5b) | día, hora y vehículo del preoperacional |

**Antigüedad máxima (N días):** `CORRECCIONES_DIAS_MAXIMOS` en `.env` (entero, **por defecto 30**, mínimo 1, máximo 365; `app/correcciones/catalogo.py::dias_maximos`). El filtro viaja en el dominio de Odoo (`search_read`), con el día de hoy y el inicio de cada jornada en hora de Colombia. El `context` devuelve `dias_maximos` y `desde` (primer día permitido) y la Mini App los usa como `min` del selector de fecha del gasto. El administrador lo amplía desde el **Panel Admin → Parámetros** (grupo «Correcciones», ver `docs/PANEL_ADMIN.md` § 6): la pestaña edita el `.env` y deja respaldo, y el bot principal toma el valor **al recrear el contenedor** (`docker compose up -d app`; un simple `restart` no relee el `.env`).

**Exclusión de lo ya corregido** (tabla `correcciones`, por `odoo_id_original`; `service._vigente`): un registro se oculta si tiene una corrección local **en curso** (pendiente, enviando o en error). Si la última corrección local está **enviada**, se oculta solo mientras el `write_date` de Odoo no sea posterior a `enviado_at` + 2 min (`ENVIADA_HOLGURA_MIN`): si Odoo lo modificó después (p. ej. nómina pidió otra corrección y volvió a `correct by`), el Post vuelve a listarse. La misma regla gobierna el **409** de «una corrección por registro». Los gastos no tienen `write_date` en la lectura: una corrección enviada los oculta siempre (el rechazado original no cambia). Una corrección `fallida` o `descartada` no oculta el registro. Los Post creados por una carga de planilla (§ 5b) se ocultan con la misma regla, por el id de Odoo creado.

## 3. Estados de Odoo

- `hr.expense.state_expenses`: `draft`, `to_approved`, `correction` (Corrección Gasto), `to_correction`, `reported`, `approved`, `done`, `refused` (Rechazado). Se corrige solo `refused`.
- `pos.operacional.state`: `draft` BORRADOR, `approved`, `approved by`, **`refused` PENDIENTE GASTOS**, **`refused by` PENDIENTE NÓMINA**, `done`, **`correct` CORRECCIÓN GASTOS**, **`correct by` CORRECCIÓN NÓMINA**, `cancelled`. Se listan y se corrigen `refused`, `refused by` y `correct by`.

## 4. Corrección de un gasto

El conductor elige un gasto; se precargan sus datos. Fijos (salen de Odoo, el cliente no los envía): `name`, `product_id`, `employee_id`. Editables: **valor** (`total_amount`, decimales con punto, máx. 2), **fecha** (no futura y dentro de los últimos `CORRECCIONES_DIAS_MAXIMOS` días; 422 si no), **descripción** (obligatoria, explica la corrección) y **soporte nuevo** (obligatorio: **PDF o foto** JPG/PNG, escaneado con la app del teléfono («📄 Abrir App para escanear», hoja con lista de apps a elegir (Drive, Adobe Scan, CamScanner; en iOS Archivos y Notas), que guarda un borrador de 2 h con el registro elegido y lo restaura al volver; `docs/GUIA_NUEVOS_BOTS.md` § 5c) o desde la galería; máx. 3 MB como Gastos Operativos).

**Fotos:** la Mini App las reduce y re-codifica a JPG en el teléfono (mismo criterio que el formulario Post-operacional y Gastos Operativos) y el servidor las convierte a PDF con `app/gastos_operativos/soporte.py::a_pdf` (orientación EXIF, transparencia sobre blanco). El formato se decide por la **firma** del archivo, no por lo que diga el cliente: PDF, JPG o PNG (HEIC no se soporta; una foto ilegible es 422). A Odoo siempre llega un PDF: `type_file` = `pdf` y `attachment_filename` con extensión `.pdf`.

Al recibir el envío el servidor **vuelve a validar la antigüedad del original** (su `date`) contra la ventana; si quedó fuera, 422.

Se **crea un `hr.expense` nuevo** (no se edita ni se enlaza el rechazado, que queda `refused`) con `POST /api/gastos/register` (`OdooRestClient.post_json_once`, sin reintento automático) y este cuerpo exacto, sin otras claves:

```json
{
  "name": "<nombre del gasto original>",
  "product_id": 1,
  "total_amount": 70000.0,
  "employee_id": 100,
  "description": "<descripción de la corrección>",
  "date": "YYYY-MM-DD",
  "state_expenses": "correction",
  "type_file": "pdf",
  "attachment_filename": "factura.pdf",
  "attachment": "<PDF en base64>"
}
```

**Deduplicación** antes de cada POST (`enviar_gasto` con `OdooWriter.buscar_gasto_corregido`): busca un `hr.expense` del empleado con `state_expenses` en (`correction`, `to_correction`), mismo producto, fecha y valor, **creado desde la recepción del envío** (2 min de holgura por relojes) y **excluyendo el id del original**: la llave natural de Gastos Operativos coincidiría con el rechazado. Si existe, se da por enviado sin postear.

Reintentos: 4xx → `fallido`; 5xx, 429 o red → `error` con backoff hasta `PREOP_MAX_ATTEMPTS`, luego `fallido`. Sin `ODOO_API_LOGIN` queda en espera sin consumir intentos.

## 5. Corrección de un Post-operacional

Se precargan los datos. Editables: **KM inicial y final** (final ≥ inicial), **entrada y salida** (hora de Colombia, UTC en Odoo; salida > entrada, máx. 24 h), **observaciones** (obligatorias; se **agregan al final** de las actuales, no las reemplazan) y **planilla** (PDF o foto, opcional: sin ella se conserva la actual; las fotos se convierten a PDF igual que en el gasto). Solo se puede corregir un Post-operacional dentro de la ventana de `CORRECCIONES_DIAS_MAXIMOS` días (por `check_in`, o `create_date` si está vacío); el servidor lo revalida al recibir (422).

**Observaciones al final:** el worker, justo antes del `write`, **relee** `observations` y escribe `actual + "\n\n" + bloque`, con `bloque` = `[Corrección AAAA-MM-DD HH:MM] <texto>` (hora de Colombia de la recepción del envío; fija, así un reintento produce el mismo bloque). Si las observaciones actuales están vacías, solo va el bloque. La pantalla muestra las observaciones actuales como referencia y avisa que el texto se agregará al final. `lunch_hour` no se captura (el formulario Post-operacional tampoco) y no se toca.

El worker **escribe sobre el mismo registro** (`write`, `OdooWriter.escribir_pos_operacional`): `km_start`, `km_end`, `check_in`, `check_out`, `observations`, (`attachment`, `attachment_filename`, `type_file` = `pdf` si hay planilla nueva) y `state` según el mapa `refused` → `correct`, `refused by` → `correct by`, `correct by` → `correct by` (un Post en «Corrección Nómina» se corrige de nuevo sobre el mismo registro y sigue ahí). Antes **relee el estado** y evalúa en este orden:

1. **Idempotencia primero:** si el estado es uno de los pendientes o corregidos, los km y horas son los de esta corrección y las observaciones actuales **contienen el bloque de esta corrección** (un envío anterior cuya respuesta se perdió) → se marca `enviado` sin escribir. El bloque nunca se agrega dos veces (tampoco en `correct by`, que es pendiente y destino a la vez);
2. el estado no es `refused` / `refused by` / `correct by` → no escribe: la corrección queda `fallido` con `descartado = true` y el motivo (estado actual); se avisa al conductor y **no se reencola**.

## 5b. Planillas no enviadas (carga de una planilla por jornada)

Sección «Planillas no enviadas» de la Mini App: jornadas del conductor, dentro de la ventana de `CORRECCIONES_DIAS_MAXIMOS` días, que **no tienen Post-operacional**. Se arman en `service._jornadas_sin_post`:

- **Jornada** = un día de Colombia con al menos un preoperacional registrado por el bot para ese empleado (`preoperacional.empleado_id`); se toma el **primero del día** (su hora y su vehículo) y se agrupa por día.
- **Se excluye** el día si: la reserva vigente del conductor (no liberada ni vencida a las 24 h) corresponde a ese día (el Post de la jornada en curso va en el formulario normal); existe un Post-operacional **local** del formulario normal (`pos_operacional`) no fallido con `check_in` ese día; existe una carga local de planilla de ese día en pendiente, enviando, error o enviada; o Odoo tiene un `pos.operacional` del empleado con `check_in` (o `create_date` si está vacío) ese día de Colombia, sea cual sea su estado. Odoo se consulta **una sola vez** por rango (`OdooWriter.pos_en_rango`) y solo si queda algún día candidato. Si esa consulta falla, `context` devuelve `planillas: []` y `planillas_mensaje` sin bloquear gastos ni Post.

Al elegir una jornada se precarga la **hora de entrada** (la del preoperacional, editable, hora de Colombia): debe caer dentro del mismo día y no ser futura (422 si no). La **planilla es obligatoria** (PDF o foto; «📄 Abrir App para escanear» y «Galería o PDF»; las fotos se convierten a PDF con `soporte.a_pdf`) y las **observaciones son obligatorias**. Al recibir, el servidor vuelve a calcular las jornadas: si el día ya no figura (409) u Odoo no responde (503, la Mini App lo trata como falta de señal).

**Se crea un `pos.operacional` nuevo** (verificado en Odoo: el registro 4779 salió de este endpoint con este mismo cuerpo, en `state` = `correct by`, `corrections_hr` verdadero, sin vehículo ni km) con `POST /api/posoperacional/register` (`OdooRestClient.post_json_once`, sin reintento automático) y este cuerpo exacto, sin otras claves (`service.build_planilla_payload`):

```json
{
  "corrections_hr": true,
  "employee_id": 100,
  "check_in": "YYYY-MM-DD HH:MM:SS",
  "observations": "<observaciones>",
  "type_file": "pdf",
  "attachment_filename": "planilla.pdf",
  "attachment": "<PDF en base64>"
}
```

**Supuesto de zona horaria (a confirmar con el primer envío real):** `check_in` se envía en **UTC** (la hora de Colombia + 5 h, como Odoo almacena las fechas; p. ej. entrada 08:07 de Colombia = `13:07:00`). La decisión vive en una sola función, `service.check_in_odoo`: si el endpoint toma el texto como hora local de Colombia, basta cambiar esa función.

**Deduplicación** antes de postear (`OdooWriter.buscar_pos_planilla`): un `pos.operacional` del empleado con `check_in` exactamente igual (UTC), creado desde la recepción − 2 min; si existe se marca `enviado` con ese id. El id de la respuesta se extrae con `extraer_id_gasto` (tolera `id`, `expense_id`, `gasto_id`, `record_id` y anidados `result`/`data`/`response`; si no lo reconoce queda `enviado` sin id y se registra la respuesta en el log). Reintentos como el gasto: 4xx → `fallido`; 5xx o red → `error` con backoff; sin `ODOO_API_LOGIN` espera sin consumir intentos.

**Persistencia:** misma tabla `correcciones` con `tipo` = `planilla` (sin columnas nuevas). `odoo_id_original` guarda el día de la jornada como entero `AAAAMMDD` (la unicidad es por empleado y día), `datos` = `{"fecha", "check_in"}`, `explicacion` = observaciones, `referencia` = `Planilla AAAA-MM-DD` y `odoo_id` = el `pos.operacional` creado. Envío sin señal (bandeja, ventana de 1 h, 410) y borrador al abrir la app de escaneo, igual que las otras secciones. Avisos al conductor: planilla recibida, enviada o fallida.

## 6. Envío sin señal

Como todos los formularios (`docs/GUIA_NUEVOS_BOTS.md` § 5): bandeja IndexedDB `correcciones-offline` en el teléfono, reenvío cada 15 s y al volver la red, ventana de 1 h desde que se perdió la señal; pasada la hora el servidor responde **410** y guarda solo la auditoría (`correcciones_envios_diferidos`, con el formulario y el PDF). Al recibir, el servidor **relee el registro en Odoo** (404 no existe, 403 es de otro empleado, 409 ya no está pendiente o ya hay una corrección vigente, 503 Odoo no responde; la Mini App trata el 503 como falta de señal y lo reintenta).

## 7. API

| Ruta | Descripción |
|---|---|
| `GET /correcciones` | Página de la Mini App |
| `GET /api/correcciones/form` | Límites (valor máx., km, texto, tamaño del PDF o foto, `dias_maximos`) |
| `GET /api/correcciones/context` | Listas en vivo: `gastos`, `pos` y `planillas` (403 sin PIN) |
| `POST /api/correcciones` | Cuerpo con `tipo` = `gasto` \| `pos` \| `planilla` (esta lleva `fecha`, `entrada`, `observaciones`, `planilla`, `planilla_nombre`, sin `odoo_id`); 202 `pendiente` (401, 403, 404, 409, 410, 422, 503) |
| `GET /api/correcciones/{uuid}` | Estado de un envío propio |

## 8. Avisos y operación

Al recibir (y al aceptar un reenvío sin señal) el bot confirma en el chat; al final avisa si quedó **enviada**, **no se aplicó** (descartada) o **falló** (la planilla no enviada tiene sus propios textos). Tablas nuevas `correcciones` y `correcciones_envios_diferidos` (las crea `create_all`).

```
python -m app.correcciones.worker --once
python -m app.correcciones.worker --listar
python -m app.correcciones.worker --requeue 3 4
python -m app.correcciones.worker --requeue-all   # no reencola las descartadas
```

## 9. Pruebas

`tests/test_correcciones.py` (fotos convertidas a PDF, ventana de días en el dominio de Odoo y 422, observaciones agregadas al final sin duplicar, payload exacto, lista y exclusión, dedupe, validaciones, Pos `correct` / `correct by` (listado y nueva corrección sobre el mismo id, reintento sin duplicar el bloque, exclusión por `write_date`), planillas no enviadas (jornadas y exclusiones, payload exacto con `check_in` UTC, dedupe, validaciones, 4xx/5xx, API), descarte e idempotencia, 4xx/5xx, API 401/403/422/409/410/503) `tests/test_admin.py` (parámetro `CORRECCIONES_DIAS_MAXIMOS`) y `tests/test_handlers.py` (botón del teclado y apertura inline).
