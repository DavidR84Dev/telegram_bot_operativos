# Combustible: Mini App de Telegram → BD local → Odoo (XML-RPC)

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
| Vehículo y contrato | El vehículo es el **reservado por el preoperacional de la jornada** (`reservas_vehiculo`) y el contrato (`agreement_id`) es el de ese preoperacional, que salió de su extracto aprobado. Sin reserva: "Primero el Preoperacional" (409). |
| Pestañas | **Combustible** / **Combustible para gastos**, como el bot de referencia → `is_fuel_expenses` de Odoo. |
| Proveedor | Lista local de **proveedores aprobados** (`proveedores_combustible`, se carga desde un CSV). Si el proveedor no está, el conductor **lo escribe** y va a Odoo como texto en `supplier`. Uno de la lista con contacto de Odoo va en `proveedor` (`res.partner`); sin contacto, su nombre va en `supplier`. |
| Factura | **Solo PDF** (firma `%PDF-`, máx. 4 MB). **Obligatoria solo en "Combustible para gastos"**; opcional en "Combustible". |
| Kilometraje | **Mayor o igual** al último registrado: el mayor entre Odoo (en vivo), la flota sincronizada y el odómetro del preoperacional de la jornada. Se valida en la Mini App y en el servidor. |
| Total | `amount` = galones × precio por galón (se muestra en vivo en la Mini App). |
| Fecha | Por defecto hoy (hora de Colombia); no futura y como máximo 30 días atrás. |
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
| `ref_invoice` | char | Referencia de la factura (opcional) |
| `image_bill` | binary ("Factura Tanqueo") | PDF en base64 |
| `attachment_filename`, `fuel_attachment` | char, boolean | Nombre del PDF y `true` cuando se adjunta |
| `name`, `currency_id` | — | Los asigna Odoo (`name` es el nombre del vehículo; no es un consecutivo) |
| `buyer`, `cash_tannage`, `lat_origin`, `long_origin` | — | No se envían |

## 3. Arquitectura y archivos

```
Conductor ──/combustible o ⛽──▶ Mini App /combustible (app/static/combustible.html)
Mini App ──initData (HMAC)──▶ POST /api/combustible ──▶ combustible + combustible_facturas (pendiente) ──▶ 202
CombustibleSender ──▶ OdooWriter.create_combustible ──▶ Odoo `fuel` ──▶ aviso al conductor
```

| Archivo | Contenido |
|---|---|
| `app/combustible/catalogo.py` | Fuente única: tipos de combustible (selection de Odoo), pestañas, rango de fecha. |
| `app/combustible/proveedores.py` | Lista de proveedores aprobados: lectura del CSV, carga (UPSERT) y CLI. |
| `app/combustible/schemas.py` | `CombustibleIn`: decimales con coma → punto, proveedor (lista o texto, exactamente uno), factura PDF por firma real y obligatoria en gastos, fecha. |
| `app/combustible/service.py` | Contexto, `submit` / `submit_diferido`, kilometraje mínimo, `build_odoo_values`, `send_one`. |
| `app/combustible/router.py` · `worker.py` | Rutas y cola (`CombustibleSender`, CLI). |
| `app/preop/models.py` | Tablas `proveedores_combustible`, `combustible`, `combustible_facturas`, `combustible_envios_diferidos`. |
| `app/preop/odoo_writer.py` | `create_combustible` (external id `combustible_<uuid>`; llave natural `employee_id` + `vehicle_id` + `km_tanking` en los últimos 2 min) y `buscar_contacto` (solo lectura, para la carga de proveedores). |

### Tablas

- **`proveedores_combustible`:** `nombre`, `clave` (nombre normalizado, única), `nit` (solo dígitos, único),
  `odoo_partner_id` y `activo`.
- **`combustible`:** `uuid` (único), identidad de Telegram, `empleado_id`, `vehiculo_id`, `preop_id`,
  `contrato_odoo_id`, `fecha_transaccion`, `para_gastos`, proveedor (`proveedor_id`, `proveedor_nombre`,
  `proveedor_odoo_id`), `galones`, `precio_galon`, `total`, `tipo_combustible`, `kilometraje`, `ref_factura` y la
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
     fecha de la transacción.
   - *Detalles:* galones y precio por galón (coma → punto al escribir), **total en vivo** y tipo de combustible
     (Diesel / ACPM por defecto).
   - *Referencias:* kilometraje (solo dígitos, con el último registrado como guía), referencia de la factura y
     **factura PDF** (obligatoria en "para gastos").
2. **Resumen:** todos los datos y el total. **Enviar registro** pide confirmación con el valor.

"Revisar →" nunca queda gris sin explicación: si falta algo, lista todo lo pendiente y cada elemento lleva a su
campo. Pantallas de bloqueo: **Primero el Preoperacional** y usuario no registrado.

## 5. Rutas

| Ruta | Respuesta |
|---|---|
| `GET /combustible` | HTML (sin caché; logo versionado) |
| `GET /api/combustible/form` | tipos, pestañas, rango de fecha y **proveedores aprobados activos** |
| `GET /api/combustible/context` | `empleado`, `contrato`, `vehiculo` reservado (o `null`), `ultimo_km` y `hoy`; 403 sin PIN |
| `POST /api/combustible` | **202**; 401 firma, 403 sin empleado, 409 sin preoperacional / vehículo distinto / uuid ajeno, 422 validación o kilometraje menor, **410** reenvío sin señal vencido |
| `GET /api/combustible/{uuid}` | estado de un envío propio |

## 6. Operación

| Tarea | Comando |
|---|---|
| Cargar proveedores aprobados | Copiar el CSV al contenedor y `docker compose exec app python -m app.combustible.proveedores cargar /tmp/proveedores.csv --resolver-odoo` (ver plantilla `docs/proveedores_combustible_ejemplo.csv`) |
| Reemplazar la lista completa | Igual, con `--reemplazar` (desactiva los que no vengan en el archivo) |
| Ver la lista | `docker compose exec app python -m app.combustible.proveedores listar` |
| Quitar un proveedor | `docker compose exec app python -m app.combustible.proveedores desactivar ID` |
| Cola | `SELECT sync_status, count(*) FROM combustible GROUP BY 1;` |
| Envíos sin señal | `SELECT estado, uuid, sin_senal_desde, recibido_at, motivo FROM combustible_envios_diferidos ORDER BY recibido_at DESC;` |
| Procesar la cola | `docker compose exec app python -m app.combustible.worker --once` |
| Reencolar fallidos | `docker compose exec app python -m app.combustible.worker --requeue-all` |

El CSV lleva encabezado `nombre;nit;odoo_partner_id` (separado por `;` o `,`, UTF-8). Solo `nombre` es
obligatorio. Se actualiza por NIT o, sin NIT, por nombre normalizado. `--resolver-odoo` busca en Odoo (solo lectura)
el contacto de las filas sin `odoo_partner_id`, por NIT y luego por nombre exacto, y solo lo asigna si es único;
al final lista los proveedores que quedaron sin contacto (irán como texto en `supplier`).

## 7. Pendientes

- [ ] Cargar la lista real de proveedores aprobados (hoy la tabla está vacía: el conductor puede escribirlos).
- [ ] Prueba de punta a punta en qa2 y desde un móvil (crea un registro real en `fuel`).
- [ ] Confirmar si el kilometraje del tanqueo debe actualizar el odómetro del vehículo en Odoo (hoy no se escribe en `fleet.vehicle.odometer`).
- [ ] Ubicación del tanqueo (`lat_origin` / `long_origin`): hoy no se envía.
