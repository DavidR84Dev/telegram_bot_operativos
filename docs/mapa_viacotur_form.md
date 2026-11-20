# Mapeo del bot Telegram `@viacoturform_bot` (viacotur-form)

- **Fecha del mapeo:** 2026-11-20
- **Método:** exploración manual en Telegram Web (`web.telegram.org/k/#@viacoturform_bot`), abriendo cada formulario sin enviar datos.
- **Usuario con el que se probó:** conductor validado (CONDUCTOR DE PRUEBA), jefe responsable RESPONSABLE DE PRUEBA, contrato CLIENTE X. **No tiene vehículo asignado**, lo que condiciona algunos formularios (ver más abajo).
- **Alcance / límites:** NO se enviaron formularios, ni ubicación, ni se tocó "Finalizar". Los pasos de captura de fotos y los selects nativos no se pudieron inspeccionar a fondo (ver "Pendientes").

---

## 1. Visión general del bot

Bot de Telegram de **VIACOTUR S.A. (Transporte especial y de carga)** para que los conductores diligencien formularios operativos. Cada formulario es una **Telegram Web App** (mini-app embebida) que se abre dentro del chat; el bot actúa como menú y como receptor de ubicación.

### Identidad visual
- Avatar: logo VIACOTUR. Etiqueta "bot".
- Las web apps usan el logo de VIACOTUR S.A. y paletas distintas según el formulario (azul/cian en Preoperacional y Prueba de Ruta, azul oscuro en AST, azul claro/pastel en Pos-Operacional, degradado índigo en Gastos Operativos, rosa en pantalla de error de Combustible). **No hay un estilo único entre formularios** (ver recomendaciones).

### Comandos del bot
- Solo uno registrado en el menú: `/start` — "Iniciar el bot - Menú principal".

---

## 2. Flujo conversacional del chat

1. Usuario envía `/start`.
2. Bot responde: **"Bienvenido 👋 / Elige una opción:"** con un teclado de respuesta (reply keyboard) que incluye el botón **"📋 Formularios"**.
3. Usuario toca "Formularios" (se envía como mensaje de texto).
4. Bot responde **"📂 Formularios disponibles:"** con un teclado inline de 7 botones:

| # | Botón | Tipo | Comportamiento |
|---|-------|------|----------------|
| 1 | ✅ Preoperacional | Web App (icono ▢ en la esquina) | Abre "Inspección Preoperacional" |
| 2 | 🗺️ Prueba de Ruta | Web App | Abre "Prueba de Ruta de Inspecciones Preoperacionales" |
| 3 | ⚠️ AST | Web App | Abre "Análisis Seguro de Trabajo" |
| 4 | 🧾 Pos-Operacional | Web App | Abre "Registro Pos-Operacional" |
| 5 | ⛽ Combustible | Web App | Abre formulario de combustible (en esta prueba devolvió error) |
| 6 | 💰 Gastos Operativos | Web App | Abre "Formulario de Gastos Operativos" |
| 7 | 🔴 Finalizar | Callback (sin icono Web App) | **No probado** (probable cierre de jornada/sesión) |

5. Al abrir **Gastos Operativos**, el bot además envía el mensaje:
   > 📋 **Instrucciones para Gastos Operativos:**
   > 1️⃣ Selecciona 💰 Gastos Operativos arriba
   > 2️⃣ Llena y envía el formulario
   > 3️⃣ Regresa aquí y toca 📍 **Enviar ubicación**
   > ⏱️ Tienes **10 minutos** para enviar la ubicación

   Tras ese mensaje queda un teclado persistente con el botón **"📍 Enviar ubicación"** (solicita la ubicación en tiempo real/única al usuario).

> **Regla de negocio detectada:** el gasto operativo se vincula a una ubicación que el conductor debe enviar por el chat dentro de una ventana de 10 minutos tras enviar el formulario. Si no hay vehículo asociado, el gasto "se registrará sin ubicación del vehículo" (aviso en el formulario).

---

## 3. Detalle por formulario

### 3.1 ✅ Preoperacional — "Inspección Preoperacional"
Wizard de **4 pasos** (barra de progreso con círculos): `1 Información General → 2 Verificación de Elementos → 3 Evidencia Fotográfica → 4 Resumen`.

**Paso 1 – Información General (observado):**
- **Conductor** (solo lectura, autocompletado desde el perfil de Telegram/registro).
- **Responsable Jefe** (solo lectura, autocompletado).
- Banner verde: *"Conductor validado con extractos aprobados"* (validación previa contra el sistema de registro de conductores).
- **Contrato** — lista desplegable "Selecciona un contrato…" (obligatoria; el botón **Siguiente →** permanece deshabilitado/atenuado hasta elegir).
- Botón **Siguiente →**.

**Pasos 2–4: no explorados** (requieren seleccionar contrato y, probablemente, cargar fotos). Del título se infiere: checklist de elementos del vehículo, fotos de evidencia y pantalla resumen antes de enviar.

### 3.2 🗺️ Prueba de Ruta — "Prueba de Ruta de Inspecciones Preoperacionales"
Wizard de **6 pasos**: `1 Información → 2 Datos Iniciales → 3 Cuestionario → 4 Cuestionario → 5 Odómetro Final → 6 Resumen` (las etiquetas aparecen truncadas en pantalla; los nombres de los pasos 3 y 4 empiezan por "Cuestionari…").

**Paso 1 – Información de la Prueba de Ruta:** texto informativo. Explica que, por novedades no detectables visualmente, se hace una prueba de ruta con **foto de kilometraje inicial y final**. Procedimiento listado (13 pasos):
1. Ingresar al vehículo
2. Ajuste de silla
3. Ajuste de cinturón
4. Encendido del vehículo
5. Girar las ruedas hasta tope derecha e izquierda (vidrios abajo)
6. Iniciar marcha midiendo a qué distancia del pedal de embrague inicia movimiento el vehículo
7. Freno fuerte para detectar sonidos iniciales
8. Giro a la izquierda de 45º (escuchar sonidos)
9. Giro a la derecha de 45º (escuchar sonidos)
10. Pendiente: frenar a mitad de camino y verificar que el vehículo se sostenga con el pedal presionado
11. Arranque en pendiente para evaluar potencia
12. Pendiente de bajada: verificar que el freno sostenga el vehículo
13. Finalizar prueba de ruta (registro fotográfico)

Botones: **Anterior** / **Siguiente >**.

**Paso 2 – (observado):**
- Título "Información General".
- **Empleado** — select que carga dinámicamente ("Cargando empleados…").
- **Foto del Odómetro Inicial** — zona punteada "Haga clic para tomar una foto" (captura de cámara).
- Botones Anterior / Siguiente.

**Pasos 3–6: no explorados.** Se infiere cuestionarios de la prueba, foto de odómetro final y resumen.

### 3.3 ⚠️ AST — "Análisis Seguro de Trabajo"
Formulario de **una sola página** con scroll. Subtítulo: *"Complete todos los campos para verificar las condiciones de seguridad"*.

**Sección "Información del Personal":**
- Empleado (select cargado dinámicamente; queda preseleccionado y bloqueado)
- Contrato (solo lectura: CLIENTE X)
- Responsable (solo lectura: RESPONSABLE DE PRUEBA)

**Sección "Verificación de Seguridad"** — preguntas con radio **Conforme / No conforme** (tarjetas de colores alternados azul/verde):
1. ¿Está en condiciones físicas y psicológicas para ejecutar la tarea sin fatiga, trastorno del sueño, problemas personales críticos u otra condición de salud que ponga en riesgo su integridad y la de los demás en la vía?
2. ¿Cuenta con los EPP y están en perfectas condiciones para cumplir con el desarrollo de la operación?
3. ¿Conoce, identifica los peligros y riesgos a los que se encuentra expuesto y aplica las medidas de intervención establecidas?
4. ¿Es consciente de que debe realizar pausas activas y con mayor énfasis en recorridos de más de 2 horas de conducción?
5. ¿Conoce los procedimientos a seguir en caso de accidente de tránsito y accidente laboral?
6. ¿Conoce, identifica los aspectos e impactos ambientales y aplica las medidas de control establecidas?
7. ¿Es consciente de la conservación del medio ambiente y los recursos naturales, siguiendo las recomendaciones del Sistema de Gestión Ambiental?
8. *(tarjeta amarilla)* ¿Ha consumido medicamentos que puedan generar somnolencia, disminución de reflejos u otros efectos secundarios...? En caso afirmativo, indique el nombre del medicamento. — radio **Sí / No**.

**Cierre:** campo **OBSERVACIONES** (textarea) y botón verde **Enviar Formulario**.

> Pendiente por confirmar: si "No conforme" o "Sí" en medicamentos despliega un campo adicional (ej. nombre del medicamento) o bloquea el envío.

### 3.4 🧾 Pos-Operacional — "Registro Pos-Operacional" ("Sistema de Control Operativo")
Al abrir muestra una pantalla de carga **"Validando acceso… Verificando que estés registrado como empleado en el sistema"** (tarda ~10–15 s) y luego el formulario. Una sola página con secciones coloreadas:

- **Información del Personal:** Empleado (autocompletado + texto verde "✓ Usuario validado: …"), Contrato (solo lectura, con icono de info).
- **Información del Vehículo:** Vehículo Asignado (aquí mostró "No hay vehículo asignado"), **KM Inicial** (numérico, 0), **KM Final** (numérico, 0).
- **Fechas y Horarios:** **Entrada** (fecha dd/mm/aaaa + hora) y **Salida** (fecha + hora).
- **Planilla de Servicios:** "Cargar Planilla (PDF o Imagen)" — selector de archivo.
- **Observaciones:** textarea "Ingrese sus observaciones aquí…".
- Botón azul **ENVIAR**.

### 3.5 ⛽ Combustible — "Registro de Combustible" ("Sistema de Gestión – Viacotur S.A")
**Primer intento:** pantalla de **error** — *"No se encontró un vehículo asignado para este usuario"* con botón **Reintentar**.
**Segundo intento (a petición del usuario):** cargó correctamente (pantalla "Cargando datos…" ~15 s) y mostró vehículo asignado **Marca/Modelo/ABC123**. 
**Hallazgo:** el comportamiento es **inconsistente** para el mismo usuario (Pos-Operacional y Gastos también dijeron "sin vehículo" minutos antes). Probable causa: fallo/lentitud intermitente en la consulta de vehículo asignado, no ausencia real de vehículo. Conviene revisar ese endpoint y mostrar un error más claro.

Formulario de una página, bienvenida "Bienvenido, CONDUCTOR DE PRUEBA", tarjeta **"Información de Compra"**:
- **Información General:** Comprador (solo lectura), Vehículo Asignado (solo lectura, con "Matrícula: ABC125" — nótese que difiere de la placa ABC123 del campo principal: **posible inconsistencia a verificar**), **Proveedor** (toggle: `Combustible` | `Combustible para gastos` + buscador "Buscar o escribir proveedor…"), **Fecha de Transacción** (date, hoy por defecto).
- **Detalles de Combustible:** Galones (ej. 12.50), Precio por Galón (ej. 12000,00), **Tipo de Combustible** (select, valor por defecto "Diesel / ACPM").
- **Referencias y Documentos:** **Kilometraje al Tanqueo** (numérico; ayuda: *"Debe ser mayor a 250000 kilómetros (último registrado)"*), **Cargar Factura** (archivo).
- Botón **Enviar Registro**; pie "Viacotur S.A – Sistema de Gestión de Combustible © 2025".
- No se vio total calculado (galones × precio) en pantalla.

### 3.6 💰 Gastos Operativos — "Formulario de Gastos Operativos"
Carga con encabezado de marca VIACOTUR S.A. (degradado índigo).

- **Tarjeta resumen:** "Gastos Operativos — Gestión de gastos empresariales" con **Total General $ 0** (se actualiza al agregar gastos).
- **Conductor** (solo lectura).
- **Fecha del Gasto** (date picker, por defecto la fecha actual: 20/11/2026).
- **Aviso amarillo:** *"No se encontró un vehículo asociado a tu usuario. El gasto se registrará sin ubicación del vehículo."*
- Botón punteado **"+ ¿Gastos Operativos?"** → agrega una tarjeta **"Gasto Operativo #N"** (con botón rojo de eliminar 🗑).
  - **Tipo de Gasto \*** (select obligatorio). En esta prueba la única opción seleccionable fue **Alimentación**; el selector nativo no pudo desplegarse visualmente, por lo que **puede haber más tipos** (ver pendientes).
  - Al elegir **Alimentación** aparece "Información de Alimentación":
    - **Valor total facturas alimentación \*** (numérico; placeholder 65000; muestra el valor formateado en moneda abajo, p. ej. "$ 0").
    - **Factura/Soporte alimentación \*** (carga de archivo).
- Botones fijos al pie: **Cancelar** y **Enviar** (barra pegajosa).
- Tras enviar, el usuario debe volver al chat y usar **📍 Enviar ubicación** (≤ 10 min).

### 3.7 🔴 Finalizar
Botón callback del menú. **No probado** (podría cerrar la jornada, sesión o limpiar el estado; riesgo de efecto real). Pendiente de definir con el equipo.

---

## 4. Patrones transversales

| Aspecto | Hallazgo |
|---|---|
| Identidad del usuario | Se autovalida contra el sistema (empleado registrado + "extractos aprobados"); campos de persona vienen precargados y bloqueados. |
| Datos precargados | Conductor, Jefe/Responsable, Contrato, Empleado, Vehículo asignado, Fecha actual. |
| Dependencia de vehículo | Combustible: bloqueante cuando no encuentra vehículo (error intermitente, ver 3.5). Pos-Operacional y Gastos: tolerante (muestra aviso/valor vacío). Preoperacional/Prueba de ruta: no determinado. El usuario sí tiene vehículo (ABC123/ABC125), por lo que el "sin vehículo" parece un fallo de carga. |
| Evidencia | Fotos por cámara (odómetro inicial/final, evidencia fotográfica), carga de archivos PDF/imagen (planilla, factura/soporte). |
| Validaciones visibles | Campos obligatorios con `*`; "Siguiente" deshabilitado hasta completar contrato (Preoperacional). |
| Salir del formulario | Al cerrar con ✕ Telegram pregunta "Changes that you made may not be saved" (Cancelar / Close anyway). |
| Latencia | Pantallas "Cargando empleados…" y "Validando acceso…" de varios segundos. |
| Ubicación | Solo se pide en el flujo de Gastos Operativos vía botón del chat (ventana de 10 min). |
| Idioma | Español (Colombia). Fechas dd/mm/aaaa, moneda COP. |

---

## 5. Observaciones y posibles mejoras (para el bot que se está construyendo)

1. **Consistencia visual:** cada formulario tiene un look distinto (colores, tipografía, títulos). Conviene un sistema de diseño común (header con logo, paleta, botones).
2. **Etiquetas truncadas** en el stepper de Prueba de Ruta ("Cuestionari…", "Odómetro F…") — acortar o hacer scroll/abreviar.
3. **Mensaje de error de Combustible:** explicar qué hacer (contactar al responsable para asignar vehículo) en lugar de solo "Reintentar".
4. **Texto "¿Gastos Operativos?"** en el botón de agregar es poco claro; algo como "+ Agregar gasto" sería más entendible.
5. **Menú de comandos:** solo `/start`; considerar `/formularios`, `/ayuda`, `/estado`.
6. **Ubicación en 10 min:** definir y comunicar qué ocurre si vence el tiempo, y confirmar con un mensaje de éxito cuando se recibe.
7. **Confirmaciones:** no se observó el mensaje de confirmación tras enviar (no se envió nada en esta prueba).
8. **"Finalizar"** debería tener texto/confirmación que explique su efecto.

---

## 6. Pendientes / preguntas abiertas

1. ¿Qué hace exactamente **🔴 Finalizar**? (no se tocó por posible efecto real).
2. ¿Por qué la consulta de vehículo asignado falla de forma intermitente (Combustible, Pos-Operacional, Gastos) y por qué la matrícula difiere (ABC123 vs ABC125) en Combustible?
3. ¿Cuáles son todas las opciones de **Tipo de Gasto** (además de Alimentación) y los campos de cada una?
4. ¿Cuál es la lista de **Contratos** del desplegable de Preoperacional y los **pasos 2–4** (checklist de elementos, fotos requeridas, resumen)?
5. ¿Cuáles son los pasos **3–6 de la Prueba de Ruta** (cuestionarios y odómetro final)?
6. ¿Qué respuesta da el bot **tras enviar** cada formulario (mensajes de éxito/error, notificaciones al jefe)?
7. ¿Qué pasa con **"No conforme"** en AST (notificación, bloqueo)?
8. Bots relacionados visibles en la lista de chats: `viactour_admin` ("Selecciona un formulario:"), `registro_viaconductor` (/start), `OperativosViacotur`, `Tickets_Reserva`. **No se exploraron**; probablemente forman parte del mismo ecosistema (registro de conductores, administración, tickets).

---

## 7. Estado de la sesión de prueba

- Ningún formulario fue enviado, no se compartió ubicación y no se pulsó "Finalizar".
- Durante la exploración se añadió accidentalmente una reacción ❤️ al mensaje de instrucciones del bot; se **retiró de inmediato** y el chat quedó como estaba.
