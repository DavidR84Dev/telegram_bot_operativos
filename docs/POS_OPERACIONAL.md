# Pos-Operacional: Mini App de Telegram → BD local → `pos.operacional` de Odoo (XML-RPC)

Módulo `app/pos_operacional/` de `telegram_bot_operativos`. Al terminar la jornada el conductor registra su cierre
(kilómetros, entrada, salida y planilla de servicios) desde la Mini App **🧾 Pos-Operacional** (sexto botón del panel
📋 Formularios, **justo debajo de 💰 Gastos Operativos**) o con `/pos_operacional`. Sigue el patrón de paquete de los
demás formularios (schemas, service, router, worker y CLI, cola local con backoff, `initData` firmado y envío sin
señal) y escribe en Odoo por XML-RPC con `OdooWriter`, igual que el preoperacional (`docs/PREOPERACIONAL.md`).

Referencia funcional: `docs/mapa_viacotur_form.md` § 3.4. Convenciones: `docs/GUIA_NUEVOS_BOTS.md` (decimales con punto,
obligatorios, envío sin señal).

---

## 1. Flujo

```
Conductor ──🧾 / /pos_operacional──▶ Mini App /pos-operacional (app/static/pos_operacional.html)
Mini App ──initData (HMAC)──▶ POST /api/pos-operacional ──▶ pos_operacional (pendiente) ──▶ 202
Bot ──▶ "🧾 Pos-operacional recibido" (+ recordatorio de 🔴 Finalizar)
PosOperacionalSender ──▶ búsqueda de duplicado (XML-RPC) ──▶ create en pos.operacional ──▶ "registrado en Odoo #id" / "no pudo guardarse"
```

1. **Información del personal.** Empleado (de solo lectura) y **contrato** (de solo lectura). El contrato lo resuelve
   el servidor y es **exactamente el del preoperacional** de la reserva (regla 2026-11-26: `preoperacional.contrato_id`
   y `pos.operacional.agreement_id` apuntan al mismo modelo `agreement`). Sin contrato en el
   preoperacional va vacío; no se deduce del extracto ni de la ficha del empleado. El cliente **nunca** envía contrato ni vehículo.
2. **Reserva vigente y vehículo.** El vehículo es el reservado al conductor por su preoperacional (`reservas_vehiculo`). **Reserva vigente (regla 2026-11-26).** Exige una reserva vigente de vehículo, es decir, un preoperacional registrado en la jornada: existe en `reservas_vehiculo` para el id de Telegram, no se liberó con 🔴 Finalizar y no venció (`RESERVA_HORAS` = 24 h). **No** exige que la fecha sea hoy (un turno nocturno hace su Pos-Operacional pasada la medianoche). Sin ella el `context` y el `POST` responden **409** con `X-Error-Code: sin_reserva_vigente` y «Para usar este formulario primero debes registrar el ✅ Preoperacional de hoy (vehículo reservado).»; la Mini App muestra la pantalla de bloqueo **Primero el Preoperacional** con «Volver al chat». Helper único: `reservas.reserva_vigente` (y `preop_service.exigir_reserva`). Envío sin señal: se acepta si la reserva estaba vigente **al abrir** el formulario (`apertura`, firmada por Telegram) según `reservas_historial` (`reservas.reserva_al_instante`), aunque ya se haya liberado al llegar; si no, 409. (Antes, sin reserva el registro iba sin vehículo: se quitó.)
3. **KM inicial** (precargado con el odómetro del último preoperacional de la jornada; editable) y **KM final**, ambos
   obligatorios, enteros o decimales con punto (`normalizarDecimal`, `parseNum`, `fmtNum`; el servidor acepta además
   `"1510,5"` y lo vuelve `1510.5`). **KM final ≥ KM inicial**. Rango 0 a 9 999 999.
4. **Fechas y horarios** (hora de Colombia en la pantalla): **Entrada** (por defecto el inicio de la reserva,
   `reservado_at`) y **Salida** (por defecto ahora). Ambas obligatorias; **Salida > Entrada** y como
   máximo **24 horas** entre las dos.
5. **Planilla de servicios**, obligatoria: «📄 Abrir App para escanear» (hoja con la lista de apps de escaneo: Drive, Adobe Scan y
   CamScanner en Android; Archivos, Notas, Adobe Scan y CamScanner en iOS; el conductor elige y se recuerda la última; antes de salir se guarda un borrador de 2 h que se restaura al volver, ver
   `docs/GUIA_NUEVOS_BOTS.md` § 5c) y luego 🖼️ galería o PDF. Las fotos se reducen en el teléfono (lado máximo 1 800 px) y se
   vuelven a codificar como **JPG**, y el servidor las **convierte a PDF** (Odoo solo recibe PDF, regla 2026-11-26);
   un PDF pasa tal cual. Máximo **3 MB**.
6. **Observaciones**, opcionales (máximo 1 000 caracteres).
7. **Enviar** (barra fija). Si falta algo, el aviso de pendientes lista todo y cada elemento lleva a su campo.
8. **No se pide ubicación** y el envío **no libera** la reserva: el conductor debe tocar 🔴 Finalizar al terminar la
   jornada (la Mini App y los mensajes del chat se lo recuerdan).

## 2. Campos enviados a Odoo (`pos.operacional`, `create` por XML-RPC)

```python
{"employee_id": 100, "agreement_id": 55, "vehicle_id": 10, "km_start": 1510.0, "km_end": 1600.5,
 "check_in": "2026-11-26 13:00:00", "check_out": "2026-11-26 22:30:00",
 "attachment": "<base64>", "attachment_filename": "Planilla 26-11.pdf", "type_file": "pdf",
 "observations": "Sin novedades"}
```

| Clave | Origen |
|---|---|
| `employee_id` | Id Odoo del empleado (PIN = id de Telegram) |
| `agreement_id` | Contrato de la jornada (ver § 1, punto 1). **Se omite** si no hay |
| `vehicle_id` | Vehículo reservado (id Odoo de `fleet.vehicle`). Siempre va (los registros anteriores al 2026-11-26 pueden no tenerlo y se omite) |
| `km_start`, `km_end` | `float` de Python (XML-RPC los serializa con punto). `km_traveled` lo calcula Odoo |
| `check_in`, `check_out` | Entrada y Salida en **UTC naive** `YYYY-MM-DD HH:MM:SS` (la hora de Colombia, UTC-5, + 5 h) |
| `attachment` | Planilla **en PDF** y base64, sin prefijo `data:` (una foto se convierte a PDF de una página en el servidor) |
| `attachment_filename` | Nombre original sin ruta y con extensión `.pdf` (`planilla.pdf` si no trae nombre) |
| `type_file` | Siempre `pdf` |
| `observations` | Texto del conductor (`""` si va vacío; la clave siempre va) |

**No se envían:** `lunch_hour` (Odoo lo deja en 1.0), `state` (Odoo lo pone en `draft`), `company_id`, ni las secciones de
gastos y combustible del modelo (`expenses`, `feeding_*`, `lodging_*`, `tolls_*`, `others_*`, `fuel_*`, `gallons`,
`km_fuel`): tienen sus propios formularios (`docs/GASTOS_OPERATIVOS.md`, `docs/COMBUSTIBLE.md`).

## 3. Validaciones

| Regla | Mini App | Servidor (`PosOperacionalIn`) |
|---|---|---|
| KM inicial y final obligatorios, 0 a 9 999 999 | pendientes | 422 |
| KM final ≥ KM inicial | pendientes | 422 «El KM final no puede ser menor que el KM inicial» |
| Entrada y Salida obligatorias | pendientes | 422 |
| Salida > Entrada | pendientes | 422 «La salida debe ser posterior a la entrada» |
| Salida − Entrada ≤ 24 h | pendientes | 422 «… no pueden pasar más de 24 horas» |
| Planilla obligatoria, PDF/JPG/PNG por firma, ≤ 3 MB | tipo y peso | 422 (`app/gastos_operativos/soporte.py`: base64, tamaño y firma; HEIC se rechaza con mensaje claro) |
| Campos desconocidos (`contrato`, `vehiculo`…) | — | 422 (`extra="forbid"`) |
| Empleado con PIN | contexto 403 | 403 al enviar |

**Zona horaria.** La Mini App envía `entrada` y `salida` como `YYYY-MM-DDTHH:MM` (hora de Colombia, sin convertir en el
teléfono, así no depende de su zona). El servidor interpreta una fecha sin zona como UTC-5, convierte una con zona y
guarda UTC. Lo que se compara (Salida > Entrada y el rango) se compara ya en UTC.

## 4. Dedupe y estados

Antes del `create` se busca por XML-RPC (solo lectura, `OdooWriter.buscar_pos_operacional`) un `pos.operacional` con
`employee_id` + `check_in` + `check_out`. Si existe se toma su id y no se crea otro: así un reintento tras un timeout (Odoo
lo creó pero la respuesta se perdió) no duplica. Además `create_pos_operacional` usa el flujo común de `OdooWriter._create`
(external id `telegram_bot_operativos.pos_operacional_<uuid>` si el usuario técnico puede registrarlo; llave natural en los
reintentos). Si la búsqueda falla de forma permanente se deja un `WARNING` y se crea igual; si falla por red se reintenta.

Estados (`pos_operacional.sync_status`, los mismos de los demás formularios):

| Estado | Significado |
|---|---|
| `pendiente` | Recibido; el worker lo toma de inmediato (no espera ubicación) |
| `enviando` | Reclamado por el worker (lease `PREOP_SENDING_LEASE_SECONDS`) |
| `enviado` | Creado en Odoo (`odoo_id`, `odoo_name` si Odoo devuelve `name`) |
| `error` | Fallo reintentable (red, timeout, 5xx, bloqueo en BD, autenticación); `next_attempt_at` con backoff |
| `fallido` | Odoo rechazó los datos (**Fault** de validación: sin reintento) o se agotaron `PREOP_MAX_ATTEMPTS` |

Mensajes al conductor por el chat: **recibido** (al aceptar el envío, también el reenvío sin señal), **registrado en
Odoo** con su id (`📄 Registro #id`) o **no pudo guardarse** con el motivo de Odoo y la referencia local. Los dos
últimos y el de recibido recuerdan tocar 🔴 Finalizar.

## 5. Envío sin señal

Regla de todos los formularios (`docs/GUIA_NUEVOS_BOTS.md` § 5): bandeja IndexedDB propia `pos-operacional-offline`
(respaldo `localStorage`) con la planilla incluida, reenvío cada 15 s y al evento `online`, ventana de **1 h** desde la
pérdida de señal. El cuerpo acepta `diferido: {init_data_original, sin_senal_desde}`. Dentro de la ventana: flujo normal
+ fila `aceptado` en `pos_operacional_envios_diferidos`. Fuera: **410**, fila `vencido` con el formulario completo
(planilla en base64 dentro de `payload`), sin Odoo ni avisos. El reenvío lleva el mismo `uuid`: si el primer intento sí
llegó, responde como duplicado y no repite el aviso.

## 6. Rutas

| Ruta | Respuesta |
|---|---|
| `GET /pos-operacional` | HTML (sin caché; logo versionado) |
| `GET /api/pos-operacional/form` | límites: `km_max`, `rango_max_horas`, `observacion_max`, `planilla_max_bytes` |
| `GET /api/pos-operacional/context` | `empleado`, `contrato`, `vehiculo` (o `null`), `km_inicial`, `entrada` y `salida` (`YYYY-MM-DDTHH:MM`, Colombia); 403 sin PIN |
| `POST /api/pos-operacional` | **202**; 401 firma, 403 sin empleado, 409 uuid ajeno o sin reserva vigente (`X-Error-Code: sin_reserva_vigente`), 422 validación, **410** reenvío sin señal vencido |
| `GET /api/pos-operacional/{uuid}` | estado de un envío propio |

## 7. Tablas (las crea `create_all`; no se tocan tablas existentes)

- **`pos_operacional`:** `uuid` (único), identidad de Telegram, `empleado_id`, `vehiculo_id` y `contrato_odoo_id` /
  `contrato` (opcionales), `fecha` (recepción), `km_inicial`, `km_final`, `check_in`, `check_out` (UTC),
  `observaciones`, la planilla (`planilla` en `BYTEA`, `planilla_nombre`, `planilla_tipo`, `planilla_tamano`,
  `planilla_sha256`) y la auditoría de sincronización común (`sync_status`, `odoo_id`, `odoo_name`, `error_log`,
  `attempts`, `next_attempt_at`, `last_attempt_at`, `sent_at`).
- **`pos_operacional_envios_diferidos`:** trazabilidad sin señal (`aceptado` / `vencido`).

## 8. Operación (CLI)

| Tarea | Comando |
|---|---|
| Procesar la cola | `docker compose exec app python -m app.pos_operacional.worker --once` |
| Ver los últimos registros y su estado | `docker compose exec app python -m app.pos_operacional.worker --listar` |
| Reenviar uno fallido | `docker compose exec app python -m app.pos_operacional.worker --requeue ID` y luego `--once` |
| Reencolar todos los fallidos | `docker compose exec app python -m app.pos_operacional.worker --requeue-all` |
| Cola | `SELECT sync_status, count(*) FROM pos_operacional GROUP BY 1;` |
| Errores | `SELECT id, attempts, error_log FROM pos_operacional WHERE sync_status IN ('error','fallido') ORDER BY id DESC;` |
| Envíos sin señal | `SELECT estado, uuid, sin_senal_desde, recibido_at, motivo FROM pos_operacional_envios_diferidos ORDER BY recibido_at DESC;` |

`--requeue` deja `attempts = 1`: el siguiente envío vuelve a buscar el duplicado en Odoo antes de crear.

## 9. Archivos

| Archivo | Contenido |
|---|---|
| `app/pos_operacional/catalogo.py` | Límites (KM, rango de 24 h, planilla) y `form_definition`. |
| `app/pos_operacional/schemas.py` | `PosOperacionalIn`: KM con punto, fechas de Colombia a UTC, planilla por firma y reglas cruzadas. |
| `app/pos_operacional/service.py` | Contexto, contrato de la jornada, `submit` / `submit_diferido`, `build_odoo_values`, `send_one` y cola. |
| `app/pos_operacional/router.py` · `worker.py` | Rutas y cola (`PosOperacionalSender`, CLI). |
| `app/gastos_operativos/soporte.py` | Validación de la firma del archivo (reutilizada). |
| `app/preop/odoo_writer.py` | `create_pos_operacional` y `buscar_pos_operacional`. |
| `app/preop/models.py` | Tablas `pos_operacional*`. |
| `app/static/pos_operacional.html` | Mini App. |
| `tests/test_pos_operacional.py` | Pruebas del módulo y del orden del botón en el menú. |

## 10. Decisiones no especificadas y pendientes

Decisiones tomadas (confirmar si no son las deseadas):

- KM inicial es **obligatorio** (sin él no hay recorrido que validar), aunque venga vacío cuando el preoperacional no lo tiene.
- Rango máximo entre Entrada y Salida: **24 h** (igual que el vencimiento de la reserva). No se valida que las horas no
  sean futuras ni un recorrido máximo en km.
- `agreement_id` y `vehicle_id` se **omiten** (no van en `False`) cuando no hay dato.
- No se envía `company_id` (no está en el modelo según `fields_get`).
- Un segundo pos-operacional con el mismo empleado, entrada y salida se toma como el mismo (dedupe por llave natural).

Pendientes para confirmar con Odoo:

- [ ] Que `pos.operacional` acepte el `create` con solo estos campos (sin `lunch_hour`, `state` ni gastos/combustible).
- [ ] Que `agreement_id` acepte el id del contrato del preoperacional (`agreement`) y que el modelo tenga `name` (si no,
      `odoo_name` queda vacío; no afecta).
- [ ] Que una restricción de Odoo sobre `km_start` (p. ej. frente al `odometer_post` del vehículo) no rechace el
      KM inicial editado por el conductor: un rechazo deja el registro `fallido` con el motivo de Odoo.
- [ ] Prueba de punta a punta en qa2 y desde un móvil (crea un `pos.operacional` real): escaneo con Drive/Archivos y regreso a Telegram, foto de galería,
      PDF, sin reserva vigente y sin señal.
