# Prueba de Ruta: Mini App de Telegram → BD local → Odoo (XML-RPC)

Módulo `app/prueba_ruta/` de `telegram_bot_operativos`. Después del preoperacional, el conductor hace la
prueba de ruta del vehículo reservado a su nombre y la registra desde la Mini App **🗺️ Prueba de Ruta**
(tercer botón del panel 📋 Formularios, debajo de ⚠️ AST) o con `/prueba_ruta`. Sigue el mismo patrón del
preoperacional y del AST: BD local primero, cola hacia Odoo, `initData` firmado y envío sin señal.

Referencia funcional: `docs/mapa_viacotur_form.md` § 3.2. Infraestructura compartida: `docs/PREOPERACIONAL.md`.

---

## 1. Decisiones (2026-11-25)

| Tema | Decisión |
|---|---|
| Ubicación | Mini App en @OperativosViacoturBot, debajo de ⚠️ AST; comando `/prueba_ruta`. |
| **Restricción de acceso** | Solo conductores cuyo **contrato del empleado** (`id_contract` o contrato temporal, ambos `agreement`) es un acuerdo autorizado. Los demás ven **"Contrato No Autorizado"** (Mini App) y el servidor rechaza el envío (403). |
| Acuerdos autorizados | Variable **`PRUEBA_RUTA_ACUERDOS`** (nombres separados por `;`). Hoy: `MINERA DE COBRE QUEBRADONA` (id 121 en qa2) y `SERVICIOS HIDROGEOLÓGICOS INTEGRALES S.A.S.` (id 132, "Integral"). **Agregar un contrato = sumarlo a la variable y reiniciar**, sin código ni migración. |
| Comparación | Nombres normalizados: sin mayúsculas, tildes ni puntuación, sin el prefijo `[código]` de `/api/acuerdos`, y los puntos de siglas no separan (`S.A.S.` = `SAS`). Se acepta el contrato si coincide su nombre o si su id es el de un acuerdo sincronizado con un nombre de la lista. Al arrancar se registra en el log cualquier nombre de la lista sin acuerdo sincronizado (posible error de digitación). |
| Vehículo | El **reservado a su nombre por el preoperacional de la jornada** (`reservas_vehiculo`). Sin reserva (no ha hecho el preoperacional, ya tocó 🔴 Finalizar o venció a las 24 h): "Primero el Preoperacional" (409). |
| Fotos | **Odómetro inicial y final obligatorias** (son lo único que Odoo guarda del kilometraje). |
| Novedades | Las 6 preguntas son Sí / No, todas obligatorias; **"Sí" = novedad**. Se permite enviar con novedades (con confirmación): Odoo abre plan de acción y el bot avisa al jefe inmediato. |
| Sin señal | Regla de todos los formularios (`docs/GUIA_NUEVOS_BOTS.md` § 5), incluidas las fotos. |

## 2. Modelo `prueba.ruta` de Odoo (`fields_get` en qa2, 2026-11-25)

Módulos `preoperacional` y `preoperacional_inherit`; el usuario técnico puede leer y crear.

| Campo | Tipo | Lo envía el bot |
|---|---|---|
| `user_id` | many2one `hr.employee` ("Empleado") | Empleado del PIN |
| `vehicle_id` | many2one `fleet.vehicle` | Vehículo reservado por el preoperacional |
| `company_id` | many2one `res.company` | Compañía del empleado |
| `jefe_inmediato` | many2one `hr.employee` | Jefe inmediato (por el nombre del responsable, si es único) |
| `direccion`, `frenos`, `suspencion`, `pedales`, `parqueo`, `reductor` | selection `si` / `no` | Respuestas |
| `km1`, `km2` | binary | Fotos del odómetro inicial y final (base64) |
| `observaciones` | text | Observaciones finales |
| `state`, `plan_accion` | — | No se envían: los calcula Odoo (un "Sí" → no conforme con plan de acción) |
| `estado_accion`, `accion_propuesta`, `responsable_accion`, `mantenimiento` | — | Gestión del plan de acción en Odoo; no los toca el bot |

El modelo **no tiene campo de contrato**: el acuerdo con el que se autorizó queda solo en la BD del bot
(`prueba_ruta.contrato_odoo_id` y `contrato`). El consecutivo que asigna Odoo es `PRE-xxxx`.

## 3. Arquitectura y archivos

```
Conductor ──/prueba_ruta o 🗺️──▶ Mini App /prueba-ruta (app/static/prueba_ruta.html)
Mini App ──initData (HMAC)──▶ POST /api/prueba-ruta ──▶ prueba_ruta + prueba_ruta_fotos (pendiente) ──▶ 202
PruebaRutaSender ──▶ OdooWriter.create_prueba_ruta ──▶ Odoo `prueba.ruta` ──▶ aviso al conductor (y al jefe si hay novedades)
```

| Archivo | Contenido |
|---|---|
| `app/prueba_ruta/catalogo.py` | Fuente única: procedimiento (13 pasos), 6 preguntas (campo Odoo, texto, bloque del cuestionario) y fotos. |
| `app/prueba_ruta/acuerdos.py` | Normalización de nombres y `contrato_autorizado` (restricción "Contrato No Autorizado"). |
| `app/prueba_ruta/schemas.py` | `PruebaRutaIn`: preguntas obligatorias, ambas fotos (JPEG/PNG por firma real, máx. 3 MB) y `diferido`. |
| `app/prueba_ruta/service.py` | Contexto, `submit` / `submit_diferido`, `build_odoo_values`, `send_one`, aviso al jefe. |
| `app/prueba_ruta/router.py` · `worker.py` | Rutas y cola (`PruebaRutaSender`, CLI). |
| `app/preop/models.py` | Tablas `prueba_ruta`, `prueba_ruta_fotos`, `prueba_ruta_envios_diferidos`. |
| `app/preop/odoo_writer.py` | `create_prueba_ruta` (external id `prueba_ruta_<uuid>`; llave natural `user_id` + `vehicle_id` + creado en los últimos 2 min). |

### Tablas

- **`prueba_ruta`:** `uuid` (único), identidad de Telegram, `empleado_id`, `vehiculo_id`, `preop_id` (preoperacional
  que reservó el vehículo), `contrato_odoo_id`/`contrato` (acuerdo autorizado), `fecha`, `respuestas` (JSONB),
  `observaciones`, `no_conformes` (respuestas "Sí") y la auditoría de sincronización común.
- **`prueba_ruta_fotos`:** `km1` y `km2` en `BYTEA`, con `mime`, `tamano` y `sha256` (fuera de la tabla principal
  para no cargar binarios al leer la cola).
- **`prueba_ruta_envios_diferidos`:** trazabilidad sin señal (`aceptado` / `vencido`); un vencido guarda el
  formulario y las dos fotos.

## 4. Mini App (`/prueba-ruta`)

Asistente de 6 pasos como la referencia, con etiquetas cortas para que no se trunquen en el celular:
**Info → Inicio → Preg. 1 → Preg. 2 → Final → Resumen**.

1. **Info:** procedimiento de la prueba (13 pasos).
2. **Inicio:** empleado, contrato autorizado y vehículo reservado (solo lectura) y **foto del odómetro inicial** `*`.
3. **Preg. 1 / 4. Preg. 2:** 3 preguntas por pantalla, botones Sí (rojo) / No (verde) de 44 px.
5. **Final:** **foto del odómetro final** `*` y observaciones.
6. **Resumen:** datos, cada pregunta con ✔ o **!** (novedad, toca para corregir) y miniaturas de las fotos.
   **Enviar formulario** pide confirmación.

"Siguiente" nunca queda gris sin explicación: si falta algo, lista todo lo pendiente del paso y cada elemento
lleva a su campo. Las fotos se toman con la cámara dentro del formulario (`getUserMedia`; Telegram para
Android ignora `capture`) o de la galería, y se comprimen antes de enviar. Pantallas de bloqueo:
**⚠️ Contrato No Autorizado**, **Primero el Preoperacional** y usuario no registrado.

## 5. Rutas

| Ruta | Respuesta |
|---|---|
| `GET /prueba-ruta` | HTML (sin caché; logo versionado) |
| `GET /api/prueba-ruta/form` | procedimiento, preguntas y fotos |
| `GET /api/prueba-ruta/context` | `empleado`, `autorizado`, `mensaje` ("Contrato No Autorizado"), `contrato` y `vehiculo` reservado (o `null`); 403 sin PIN |
| `POST /api/prueba-ruta` | **202**; 401 firma, **403 "Contrato No Autorizado"** o sin empleado, 409 sin preoperacional / vehículo distinto / uuid ajeno, 422 validación, **410** reenvío sin señal vencido |
| `GET /api/prueba-ruta/{uuid}` | estado de un envío propio |

## 6. Operación

| Tarea | Comando |
|---|---|
| Agregar un contrato autorizado | Sumar el nombre a `PRUEBA_RUTA_ACUERDOS` en `.env` (separado por `;`) y `docker compose up -d` |
| Cola | `SELECT sync_status, count(*) FROM prueba_ruta GROUP BY 1;` |
| Envíos sin señal | `SELECT estado, uuid, sin_senal_desde, recibido_at, motivo FROM prueba_ruta_envios_diferidos ORDER BY recibido_at DESC;` |
| Procesar la cola | `docker compose exec app python -m app.prueba_ruta.worker --once` |
| Reencolar fallidos | `docker compose exec app python -m app.prueba_ruta.worker --requeue-all` |

## 7. Pendientes

- [ ] Prueba de punta a punta en qa2 y desde un móvil (crea un registro real en `prueba.ruta`).
- [ ] Si en el futuro la lista la debe administrar el negocio sin despliegues: casilla "Requiere Prueba de Ruta"
      en el acuerdo de Odoo expuesta en `/api/acuerdos` (cambio en `viacotur_api`).
- [ ] ¿Una prueba por jornada? Hoy no hay límite (igual que el bot de referencia).
