# AST (Análisis Seguro de Trabajo): Mini App de Telegram → BD local → Odoo (XML-RPC)

Módulo `app/ast_trabajo/` de `telegram_bot_operativos`. Antes de la operación, el conductor responde
el Análisis Seguro de Trabajo desde la Mini App **⚠️ AST** del panel 📋 Formularios. Igual que el
preoperacional, el formulario se guarda primero en PostgreSQL y un worker lo crea en Odoo, así que
una caída de Odoo no hace perder datos.

Referencia funcional: `docs/mapa_viacotur_form.md` § 3.3 (formulario AST de `@viacoturform_bot`).
Infraestructura compartida: `docs/PREOPERACIONAL.md` (tablas maestras, `initData`, cola, conector).

---

## 1. Decisiones (2026-11-25)

| Tema | Decisión |
|---|---|
| Ubicación | Mini App dentro de **@OperativosViacoturBot**, segundo botón del panel 📋 Formularios (`⚠️ AST`) y comando `/ast`. Módulo hermano `app/ast_trabajo/` que reutiliza `app/preop/` (BD, maestros, auth, cola y conector). |
| Preguntas | Las **10** del modelo `ast` de Odoo, incluidas `documentos` y `descanso`, que el bot de referencia no muestra. Todas **obligatorias**. |
| "No conforme" o medicamentos = "Sí" | **Se permite enviar** previa confirmación. Odoo deja el registro *no conforme* con plan de acción y el bot **avisa al jefe inmediato** por Telegram. Con medicamentos = "Sí" el **nombre del medicamento es obligatorio**. |
| Contrato y acceso | Igual que el preoperacional: contratos de los **extractos aprobados y vigentes** del conductor. Uno solo: se asigna y se bloquea; varios: el conductor elige; ninguno: pantalla de bloqueo. El AST no lleva vehículo. |

> El nombre del paquete es `ast_trabajo` y no `ast` para no confundirse con el módulo `ast` de la
> biblioteca estándar de Python.

## 2. Modelo `ast` de Odoo (verificado con `fields_get` en qa2, 2026-11-25)

Módulos `preoperacional` y `preoperacional_inherit`. El usuario técnico tiene permiso de leer, crear y
escribir (`ir.model.access` *ast*).

| Campo | Tipo | Lo envía el bot |
|---|---|---|
| `employee_id` | many2one `hr.employee` | Empleado del PIN (= id de Telegram) |
| `company_id` | many2one `res.company` | Compañía del empleado |
| `contrato_id` | many2one `agreement` | Contrato elegido (de un extracto aprobado) |
| `immediate_boss_id` | many2one `hr.employee` | Jefe inmediato, resuelto por el nombre del responsable (si es único) |
| `documentos`, `descanso`, `condiciones`, `epp`, `peligros`, `pausas`, `procedimientos`, `aspectos`, `conservacion` | selection `conforme` / `no conforme` | Respuestas |
| `medicamentos` | selection `si` / `no` | Respuesta |
| `nombre_medicamento` | char | Solo si medicamentos = `si` (si no, vacío) |
| `observaciones` | char | Texto libre (vacío si no hay) |
| `state`, `plan_accion` | — | **No se envían**: Odoo los calcula. Cualquier `no conforme` o medicamentos = `si` deja `state = no conforme` y `plan_accion = True` (comprobado con los registros existentes en qa2). |
| `responsable_id` | many2one `res.users` | No se envía (ningún registro existente lo usa) |

## 3. Arquitectura

```
Conductor ──/ast o ⚠️ AST──▶ botón inline web_app ──▶ Mini App /ast (app/static/ast.html)
Mini App ──X-Telegram-Init-Data (HMAC)──▶ POST /api/ast ──▶ tabla ast_trabajo (pendiente) ──▶ 202
AstSender (asyncio) ──claim FOR UPDATE SKIP LOCKED──▶ OdooWriter.create_ast ──▶ Odoo `ast` (AST-xxxx)
          └─▶ aviso al conductor (enviado / fallido) y, si hay alertas, al jefe inmediato
```

| Archivo | Contenido |
|---|---|
| `app/ast_trabajo/catalogo.py` | **Fuente única** de las preguntas: clave Odoo, texto, texto corto, opciones, respuesta que alerta y `obligatorio`. Lo entrega `GET /api/ast/form`. |
| `app/ast_trabajo/schemas.py` | `AstIn` (entrada estricta, `extra="forbid"`): todas las preguntas obligatorias, opciones del catálogo, nombre del medicamento si = "Sí". |
| `app/ast_trabajo/service.py` | Contexto (contratos de extractos aprobados), `submit` idempotente por `uuid`, `build_odoo_values`, `send_one`, aviso al jefe. |
| `app/ast_trabajo/router.py` | Rutas `/ast` y `/api/ast/*`. |
| `app/ast_trabajo/worker.py` | `AstSender` y CLI de operación. |
| `app/preop/models.py` → `AnalisisSeguro` | Tabla `ast_trabajo`. |
| `app/preop/odoo_writer.py` → `create_ast` | Mismo flujo que `create_preoperacional` (external id `telegram_bot_operativos.ast_<uuid>`; tras un envío incierto, llave natural = empleado + creado en los últimos 2 min). |
| `app/static/ast.html` | Mini App. |

### Tabla `ast_trabajo`

`uuid` (único, idempotencia), `telegram_user_id`, `telegram_username`, `firma_nombre`, `empleado_id`
(FK), `extracto_id` (FK, solo trazabilidad: Odoo no tiene ese campo), `contrato_odoo_id`, `fecha`,
`respuestas` (JSONB `{campo_odoo: valor}`), `nombre_medicamento`, `observaciones`, `no_conformes`
(respuestas con alerta) y la auditoría de sincronización del preoperacional (`sync_status`, `odoo_id`,
`odoo_name`, `error_log`, `attempts`, `next_attempt_at`, `last_attempt_at`, `sent_at`). Se crea sola al
arrancar (`create_all`).

## 4. Mini App (`/ast`)

Dos pasos con barra de progreso e identidad VIACOTUR (mismo encabezado y paleta del preoperacional):

1. **Verificación:** Información del personal (empleado y responsable bloqueados, contrato `*`, aviso
   "Conductor validado con extractos aprobados"), las 10 preguntas con botones **Conforme / No conforme**
   (medicamentos: **Sí / No**, en tarjeta amarilla; con "Sí" aparece *Nombre del medicamento* `*`) y
   Observaciones. "Siguiente" nunca queda gris sin explicación: si falta algo, un aviso rojo lista
   **todo** lo pendiente y cada elemento lleva a su pregunta o campo.
2. **Resumen:** información del personal, cada pregunta con ✔ o **!** rojo (alerta) y su respuesta, y
   las observaciones. Al tocar una alerta vuelve a la pregunta. **Enviar formulario** pide confirmación
   (indica cuántas alertas se informarán al jefe).

Al enviar: pantalla "AST recibido" y, en el chat, la confirmación con el número **AST-xxxx** cuando
llega a Odoo. Cerrar con cambios sin enviar pide confirmación. Los datos se pintan con `textContent`.

Sin empleado (PIN) o sin extracto aprobado se muestra una pantalla de bloqueo que explica qué hacer.

## 5. Rutas

| Ruta | Respuesta |
|---|---|
| `GET /ast` | HTML de la Mini App (sin caché; logo versionado) |
| `GET /api/ast/form` | catálogo de preguntas |
| `GET /api/ast/context` | empleado, responsable, `validado` y contratos de sus extractos aprobados; **403** si el id de Telegram no es PIN de un empleado activo |
| `POST /api/ast` | **202** `{id, uuid, sync_status: "pendiente", no_conformes, duplicado}`; 401 (firma), 403 (sin empleado o sin extracto), 409 (uuid ajeno), 422 (validación o contrato ajeno) |
| `GET /api/ast/{uuid}` | estado de un envío propio |

Seguridad: igual que el preoperacional (`initData` con HMAC-SHA256 del token del bot y antigüedad máxima
`PREOP_AUTH_MAX_AGE_SECONDS`). La identidad sale solo del `initData`.

## 6. Operación

No hay variables de entorno nuevas: usa las del preoperacional (`ODOO_WRITE_*`, `PREOP_WORKER_INTERVAL_SECONDS`,
`PREOP_MAX_ATTEMPTS`, `PREOP_SENDING_LEASE_SECONDS`, `PREOP_AUTH_MAX_AGE_SECONDS`, `PREOP_BASE_URL`).

| Tarea | Comando |
|---|---|
| Cola | `SELECT sync_status, count(*) FROM ast_trabajo GROUP BY 1;` |
| Fallidos | `SELECT id, uuid, attempts, error_log FROM ast_trabajo WHERE sync_status='fallido';` |
| Procesar la cola a mano | `docker compose exec app python -m app.ast_trabajo.worker --once` |
| Reencolar | `docker compose exec app python -m app.ast_trabajo.worker --requeue 3 4` (o `--requeue-all`) |

## 7. Pendientes

- [ ] Prueba de punta a punta en qa2 (crear un `AST-xxxx` real) y desde un móvil por el túnel.
- [ ] Envío sin señal (bandeja de salida en el teléfono, como el preoperacional): no incluido en esta
      primera versión; hoy, sin señal, la Mini App muestra el error y permite reintentar sin duplicar.
- [ ] ¿Límite de un AST por jornada o por día? Hoy no hay límite (igual que el bot de referencia).
- [ ] El jefe debe tener su id de Telegram en el PIN y haber abierto el bot para recibir el aviso.
