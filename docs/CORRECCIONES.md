# Correcciones Post y Gastos

Mini App **🛠️ Correcciones Post y Gastos** (`/correcciones`) para que el conductor corrija, desde el chat, **(1)** sus gastos operativos rechazados (`hr.expense`) y **(2)** sus Post-operacionales pendientes (`pos.operacional`). Código en `app/correcciones/`, página en `app/static/correcciones.html`.

## 1. Cómo se abre

- Botón fijo **🛠️ Correcciones Post y Gastos** en el teclado del bot (segunda fila, bajo 📋 Formularios y 📍 Enviar ubicación) y comando `/correcciones`. Ambos responden con un botón **inline** `web_app` que abre la Mini App (desde un botón de teclado no llega `initData`).
- No va en el panel 📋 Formularios (`FORMULARIOS`) y **no exige reserva vigente** del vehículo: la corrección es posterior a la jornada. Es la única excepción a la regla de `tests/test_reserva_requerida.py`.
- Un usuario sin PIN recibe el mismo trato que en los demás formularios: `_authorized` le ofrece 📝 Registro. El PIN del empleado activo es el id de Telegram (`Empleado.codigo_pin`); su `employee_id` de Odoo es `Empleado.odoo_id`.

## 2. Qué se lista (en vivo, solo lectura)

`GET /api/correcciones/context` (cabecera `X-Telegram-Init-Data`) consulta Odoo por XML-RPC (`OdooWriter.gastos_rechazados` / `pos_pendientes`, con lock y sin lanzar excepción: si Odoo falla devuelve `odoo_ok: false` con un mensaje y la Mini App lo muestra):

| Lista | Modelo y filtro | Se muestra |
|---|---|---|
| Gastos rechazados | `hr.expense`, `employee_id` = empleado, `state_expenses` = `refused`, `date` >= hoy − N días | nombre, tipo (`product_id`), valor, fecha, descripción |
| Post-operacionales pendientes | `pos.operacional`, `employee_id` = empleado, `state` en (`refused`, `refused by`), `check_in` >= hoy − N días (o, si `check_in` está vacío, `create_date`) | entrada/salida, vehículo, contrato, km, estado y las anotaciones `corrections` / `corrections_hr` |

**Antigüedad máxima (N días):** `CORRECCIONES_DIAS_MAXIMOS` en `.env` (entero, **por defecto 30**, mínimo 1, máximo 365; `app/correcciones/catalogo.py::dias_maximos`). El filtro viaja en el dominio de Odoo (`search_read`), con el día de hoy y el inicio de cada jornada en hora de Colombia. El `context` devuelve `dias_maximos` y `desde` (primer día permitido) y la Mini App los usa como `min` del selector de fecha del gasto. El administrador lo amplía desde el **Panel Admin → Parámetros** (grupo «Correcciones», ver `docs/PANEL_ADMIN.md` § 6): la pestaña edita el `.env` y deja respaldo, y el bot principal toma el valor **al recrear el contenedor** (`docker compose up -d app`; un simple `restart` no relee el `.env`).

Se excluyen los registros que ya tienen una corrección local **pendiente, enviando, en error o enviada** (tabla `correcciones`, por `odoo_id_original`). Una corrección `fallida` o `descartada` no oculta el registro.

## 3. Estados de Odoo

- `hr.expense.state_expenses`: `draft`, `to_approved`, `correction` (Corrección Gasto), `to_correction`, `reported`, `approved`, `done`, `refused` (Rechazado). Se corrige solo `refused`.
- `pos.operacional.state`: `draft` BORRADOR, `approved`, `approved by`, **`refused` PENDIENTE GASTOS**, **`refused by` PENDIENTE NÓMINA**, `done`, **`correct` CORRECCIÓN GASTOS**, **`correct by` CORRECCIÓN NÓMINA**, `cancelled`.

## 4. Corrección de un gasto

El conductor elige un gasto; se precargan sus datos. Fijos (salen de Odoo, el cliente no los envía): `name`, `product_id`, `employee_id`. Editables: **valor** (`total_amount`, decimales con punto, máx. 2), **fecha** (no futura y dentro de los últimos `CORRECCIONES_DIAS_MAXIMOS` días; 422 si no), **descripción** (obligatoria, explica la corrección) y **soporte nuevo** (obligatorio: **PDF o foto** JPG/PNG, con la cámara o desde la galería; máx. 3 MB como Gastos Operativos).

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

El worker **escribe sobre el mismo registro** (`write`, `OdooWriter.escribir_pos_operacional`): `km_start`, `km_end`, `check_in`, `check_out`, `observations`, (`attachment`, `attachment_filename`, `type_file` = `pdf` si hay planilla nueva) y `state` = `correct` si estaba `refused` o `correct by` si estaba `refused by`. Antes **relee el estado**:

- ya no es `refused` / `refused by` → no escribe: la corrección queda `fallido` con `descartado = true` y el motivo (estado actual); se avisa al conductor y **no se reencola**;
- ya está en `correct` / `correct by` con los mismos km y horas y con las observaciones actuales **conteniendo el bloque de esta corrección** (un envío anterior cuya respuesta se perdió) → se marca `enviado` sin escribir (idempotente; las observaciones ya no se comparan por igualdad). El bloque nunca se agrega dos veces.

## 6. Envío sin señal

Como todos los formularios (`docs/GUIA_NUEVOS_BOTS.md` § 5): bandeja IndexedDB `correcciones-offline` en el teléfono, reenvío cada 15 s y al volver la red, ventana de 1 h desde que se perdió la señal; pasada la hora el servidor responde **410** y guarda solo la auditoría (`correcciones_envios_diferidos`, con el formulario y el PDF). Al recibir, el servidor **relee el registro en Odoo** (404 no existe, 403 es de otro empleado, 409 ya no está pendiente o ya hay una corrección en curso, 503 Odoo no responde; la Mini App trata el 503 como falta de señal y lo reintenta).

## 7. API

| Ruta | Descripción |
|---|---|
| `GET /correcciones` | Página de la Mini App |
| `GET /api/correcciones/form` | Límites (valor máx., km, texto, tamaño del PDF o foto, `dias_maximos`) |
| `GET /api/correcciones/context` | Listas en vivo (403 sin PIN) |
| `POST /api/correcciones` | Cuerpo con `tipo` = `gasto` \| `pos`; 202 `pendiente` (401, 403, 404, 409, 410, 422, 503) |
| `GET /api/correcciones/{uuid}` | Estado de un envío propio |

## 8. Avisos y operación

Al recibir (y al aceptar un reenvío sin señal) el bot confirma en el chat; al final avisa si quedó **enviada**, **no se aplicó** (descartada) o **falló**. Tablas nuevas `correcciones` y `correcciones_envios_diferidos` (las crea `create_all`).

```
python -m app.correcciones.worker --once
python -m app.correcciones.worker --listar
python -m app.correcciones.worker --requeue 3 4
python -m app.correcciones.worker --requeue-all   # no reencola las descartadas
```

## 9. Pruebas

`tests/test_correcciones.py` (fotos convertidas a PDF, ventana de días en el dominio de Odoo y 422, observaciones agregadas al final sin duplicar, payload exacto, lista y exclusión, dedupe, validaciones, Pos `correct` / `correct by`, descarte e idempotencia, 4xx/5xx, API 401/403/422/409/410/503) `tests/test_admin.py` (parámetro `CORRECCIONES_DIAS_MAXIMOS`) y `tests/test_handlers.py` (botón del teclado y apertura inline).
