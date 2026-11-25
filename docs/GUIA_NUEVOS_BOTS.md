# Guía para nuevos bots: convenciones aprendidas en Operativos

Reglas que salieron del desarrollo de `telegram_bot_operativos` (noviembre de 2026) y que
deben aplicarse en los próximos bots de Telegram conectados a Odoo. Cada regla indica
**dónde está implementada** aquí, para copiar el patrón.

Ver también `CONTRIBUTING.md` (estructura del proyecto y § Agregar un nuevo bot operativo).

---

## 1. Números decimales: siempre punto

Odoo solo admite **punto** como separador decimal en los campos `float`. Los teclados en
español del celular escriben **coma**. Se normaliza en las dos capas:

| Capa | Regla | Implementación |
|---|---|---|
| Mini App, al escribir | La coma se cambia por punto en el mismo campo, se quitan los caracteres no numéricos y se deja un solo separador (`5,5` → `5.5`; `5.5.1` → `5.51`; `8,2mm` → `8.2`). | `normalizarDecimal(input)` en `app/static/preoperacional.html`, llamada en el evento `input` de cada campo decimal. |
| Mini App, al leer | `parseNum` reemplaza **todas** las comas por punto y devuelve `NaN` si el campo está vacío. | `parseNum` en el mismo archivo. |
| Mini App, al mostrar | Punto decimal y miles con espacio (`125 430 km`, `5.5 mm`), para que lo que ve el usuario coincida con lo que guarda Odoo. No usar `toLocaleString("es-CO")`: muestra coma decimal. | `fmtNum` en el mismo archivo. |
| Servidor | Un valor que llegue como texto con coma (`"5,5"`) se convierte a `5.5` **antes** de validar (cliente viejo en caché, formulario guardado sin señal). Un valor ambiguo (`"1,2,3"`) se rechaza (422). | `_punto_decimal` + `field_validator(..., mode="before")` en `app/preop/schemas.py`. |
| Hacia Odoo | Enviar siempre `float(...)` de Python, nunca el texto del formulario. XML-RPC serializa los `float` con punto. | `build_odoo_values` en `app/preop/odoo_writer.py`. |

Campos enteros (odómetro): aceptar solo dígitos (`onKm` quita todo lo demás). Usar
`inputmode="decimal"` en campos decimales y `inputmode="numeric"` en enteros.

Prueba de referencia: `test_decimales_con_coma_se_normalizan_a_punto` (`tests/test_preop.py`).

## 2. Campos obligatorios

1. **Una sola fuente de verdad.** El catálogo del formulario marca cada campo con
   `obligatorio` (`MeasureItem.required` y `OBLIGATORIOS` en `app/preop/checklist.py`) y
   `form_definition()` lo entrega a la Mini App. La Mini App no repite la lista.
2. **Validar en las dos capas.** La Mini App no deja avanzar y el servidor rechaza el envío
   (`PreoperacionalIn._normalize` en `app/preop/schemas.py`). Un cliente viejo o manipulado
   no debe poder saltarse la regla.
3. **Obligatorio solo si es visible.** Si un bloque depende de una respuesta (llantas
   internas 5 y 6 solo cuando "¿Tiene llantas internas?" = Sí), la regla se aplica igual en
   el cliente (`visibleMeasureInputs`) y en el servidor (`extra.llantas_internas`).
4. **Marcar con `*`** la etiqueta de cada campo obligatorio.
5. **Nunca un botón deshabilitado sin explicación.** Si "Siguiente" queda gris, el aviso de
   pendientes (`renderPending`) debe listar **todo** lo que falta (ítems y medidas), y cada
   elemento de la lista lleva al campo (`jumpToItem`, `jumpToMeasure`).
6. **Recalcular el estado en cada cambio** que afecte la regla: al escribir en un campo y al
   cambiar una respuesta que muestra u oculta campos (`refreshNext()`).

Hoy son obligatorios en el preoperacional: los 6 ítems de 3 opciones (Conforme / No
conforme / No aplica) y el **labrado y la presión** de las llantas 1–4, la de repuesto y,
si el vehículo las tiene, las internas 5 y 6.

## 3. Revisión ítem por ítem

El botón **"✔ Marcar pestaña conforme"** del preoperacional está **desactivado a propósito**
(decisión del 2026-11-24): el conductor debe revisar cada ítem. No reactivarlo, y no ofrecer
atajos de "marcar todo" en los formularios de inspección de los próximos bots.

## 4. Registro de usuarios nuevos (onboarding con Odoo)

El acceso a los bots se basa en que el **id de Telegram** esté en `hr.employee.pin`. En
lugar de rechazar a un usuario nuevo con "Acceso no autorizado", se le ofrece registrarse.
Implementación: `app/preop/registro.py` (servicio) y la conversación 📝 Registro en
`app/telegram_app.py`; pruebas en `tests/test_registro.py`.

- **Flujo:** nombre completo → número de documento → correo, un dato por mensaje, con
  validación y `/cancelar` en cualquier paso (`ConversationHandler`). Los datos y el id de
  Telegram se guardan en una tabla propia (`registros_conductor`) con un `estado`
  (`pendiente`, `registrado`, `no_encontrado`, `pin_ocupado`, `error`) y un contador de
  `intentos`. Una fila por usuario: los reintentos actualizan la misma fila.
- **Validación en Odoo:** `GET /api/empleados` buscando por `identificacion`, comparando
  solo dígitos. Si no existe: *"Conductor no registrado en Odoo. Por favor comunicarse con el
  área de Talento humano"*.
- **Registro en Odoo:** `POST /api/empleados/register` con `{id, pin}`.
  ⚠ Ese endpoint **crea un empleado nuevo** si el `id` no existe: llamarlo solo con el `id`
  de un empleado devuelto por `/api/empleados`.
- **No sobrescribir un PIN ajeno.** Si el empleado ya tiene el id de otra cuenta de
  Telegram, no se cambia y se remite a Talento humano.
- **Acceso inmediato:** tras registrar, invalidar el caché de acceso
  (`AccessControl.forget`) y actualizar la copia local de empleados, para no esperar la
  sincronización periódica.
- **Solo para usuarios nuevos:** quien ya tiene acceso, o ya completó el registro, no ve la
  opción.
- **Orden de los handlers:** la conversación se registra **antes** que los demás handlers,
  para que las respuestas del usuario no las capture el manejador de texto general.
- **Llamadas a Odoo fuera de las transacciones** de la BD local: guardar `pendiente`, llamar
  a la API y guardar el resultado en otra transacción.
- **Riesgo conocido:** hoy basta con el número de documento. Cualquiera que conozca la
  cédula de un empleado sin PIN podría asociarle su Telegram. Si un bot maneja información
  sensible, exigir además que coincida otro dato (p. ej. el correo laboral).

## 5. Envío sin señal: obligatorio en todos los formularios

Regla del usuario (2026-11-25): **todo formulario** de este proyecto, el actual y los que se agreguen,
sigue la misma directriz del preoperacional cuando el conductor se queda sin señal. Implementado en el
preoperacional, el AST, la Prueba de Ruta y Combustible.

| Capa | Regla | Implementación de referencia |
|---|---|---|
| Mini App | Si el envío falla por red (sin respuesta, o 502/503/504 del túnel), el formulario se guarda en una **bandeja temporal en el teléfono** (IndexedDB; respaldo `localStorage`) con su `uuid`, el `initData` de la apertura y `sin_senal_desde`. Base IndexedDB **propia por formulario** (`preop-offline`, `ast-offline`, `prueba-ruta-offline`, `combustible-offline`). | `bandeja`, `vaciarBandeja`, `modoSinSenal`, `vencer` en `app/static/ast.html` |
| Mini App | **Reenvío automático:** mientras siga abierta reintenta cada 15 s y al evento `online`; si se cerró, al volver a abrirla con señal envía primero lo guardado. | `reintentarSiPendiente`, `init()` |
| Servidor | El cuerpo acepta `diferido: {init_data_original, sin_senal_desde}` (`EnvioDiferidoIn`). Ventana de **1 h desde la pérdida de señal**, anclada a la hora de apertura firmada por Telegram (`app/preop/diferido.py`). | `tiempos_diferido` y `firma_previa_diferida` (`app/preop/router.py`) |
| Servidor | Dentro de la ventana: flujo normal + fila `aceptado` de trazabilidad. Fuera: **410**, fila `vencido` con el formulario completo, sin Odoo ni avisos. Tabla de auditoría propia por formulario (`preop_envios_diferidos`, `ast_envios_diferidos`, `prueba_ruta_envios_diferidos`, `combustible_envios_diferidos`). Los adjuntos (fotos, PDF) viajan en la bandeja y un vencido los conserva. | `submit_diferido` en `app/ast_trabajo/service.py` |
| Idempotencia | El reenvío lleva el mismo `uuid`: si el primer intento sí llegó, responde como duplicado. | |

Pruebas mínimas por formulario: dentro de la ventana, vencido (410 y solo auditoría), primer intento que sí
llegó (duplicado) y firmas/usuario (`tests/test_ast.py` § envío sin señal).

## 6. Lista de verificación para un bot nuevo

- [ ] Campos decimales con `normalizarDecimal` + `parseNum` + `fmtNum` (punto) y
      `_punto_decimal` en el esquema del servidor.
- [ ] Obligatorios definidos en el catálogo y validados en la Mini App y en el servidor.
- [ ] Aviso de pendientes que lista todo lo que falta y lleva a cada campo.
- [ ] Sin botones de "marcar todo conforme" en inspecciones.
- [ ] Usuarios sin PIN reciben el flujo de registro, no un rechazo.
- [ ] Envío sin señal: bandeja en el teléfono, reenvío automático y ventana de 1 h en el servidor (§ 5).
- [ ] Pruebas de cada regla (servidor) y `node --check` del JavaScript de la Mini App.
