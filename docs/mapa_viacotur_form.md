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
- **Referencias y Documentos:** **Kilometraje al Tanqueo** (numérico; ayuda: *"Debe ser mayor a 253888 kilómetros (último registrado)"*), **Cargar Factura** (archivo).
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

---

## 8. Complemento: Preoperacional en profundidad (recorrido guiado, 2026-11-23)

Recorrido hecho con el usuario guiando. No se envió el formulario; se llegó hasta el paso 2 y no se marcó ninguna casilla.

### Paso 1 – Información General (completo)
1. **Conductor** y **Responsable Jefe**: solo lectura, precargados.
2. Banner verde "Conductor validado con extractos aprobados".
3. **Contrato** (select): al elegir **CLIENTE X** aparece dinámicamente la sección **Vehículo**.
4. **Vehículo**: lista de tarjetas seleccionables filtrada por contrato. Mostró una sola: **ABC125 – Marca/Modelo – N.º interno 090 – etiqueta verde "DISPONIBLE"**. (El usuario mencionó "ABC135"; la lista solo ofrecía ABC125, se seleccionó esa. Confirmar placa correcta.)
5. Al seleccionar el vehículo se muestran **Número Interno del Vehículo** (solo lectura, 090) y **Odómetro Actual**.
6. **Odómetro Actual**: numérico, con placeholder "Último registrado: 253600 km" y guía "El último odómetro registrado fue 253600 km". Se ingresó 253600 (igual al último registrado) y el botón **Siguiente →** se habilitó. *Por confirmar:* si el sistema exige valor estrictamente mayor (en Combustible dice "debe ser mayor a 253888") o acepta igual.
7. **Siguiente →** solo se habilita con contrato + vehículo + odómetro.

Dependencias: contrato → vehículos disponibles → n.º interno + último odómetro.

### Paso 2 – Verificación de Elementos
Cuatro pestañas: **Documentos | Rines/Llantas | Equipo | Vehículo**. Botones **← Atrás** y **Siguiente →** al pie. El contenido de cada pestaña hace scroll interno (dentro de un panel con barra de desplazamiento), además del scroll de la página.

**Pestaña Documentos** — casillas (checkbox) simples:
Licencia de Conducción, Licencia de Tránsito, SOAT, Revisión Tecnomecánica, Extracto de Contrato, Tarjeta de Operación, Cédula de Ciudadanía.

**Pestaña Rines/Llantas:**
- Casillas globales: "Estado de los rines conforme", "Estado de las llantas conforme".
- Por llanta (**Llanta N.º 1 a 4**): **Labrado (mm)** (decimal, 0.0) y **Presión de aire (PSI)** (entero, 0).
- Banner naranja: **"El vehículo tiene llantas internas"** con botón **Sí** que muestra las llantas 5 y 6.
- **Llanta de Repuesto**: Labrado (mm) y Presión (PSI).

**Pestaña Equipo** ("EQUIPO DE PREVENCIÓN Y SEGURIDAD"): cada ítem indica entre paréntesis el criterio de *no conforme* (p. ej. "sin existencia, dañado"). Dos tipos de control:
- Casilla simple: Conos, Cuñas, Botiquín de primeros auxilios, Gato, Linterna, Herramientas básicas (alicate, destornilladores, llave de expansión y llaves fijas), entre otros.
- Radio **Conforme / No conforme / No aplica**: Pico y pala, Eslingas, Cables de arranque, Cámaras de vehículo (delantera y trasera), Estado de barra antivuelco; el Kit control derrames químicos solo ofrece Conforme / No conforme.
- Casillas al final: Cruceta, Profundímetro, Medidor de aire (y posiblemente más; la lista no se recorrió completa).

**Pestaña Vehículo** (agrupada por secciones con encabezado):
- **EXTINTORES:** Extintor N.º 1 (casilla); Extintor N.º 2 (radio Conforme / No conforme / No aplica).
- **CHASIS Y CABINA:** orden y aseo dentro y fuera del vehículo, cinturones de seguridad, suspensión–dirección, espejos y retrovisores, estado de inclinación del espaldar, cojinería del asiento, fijación del asiento, etc.
- **NIVELES, SISTEMAS DE DIRECCIÓN, MOTOR Y FRENOS:** nivel de líquido de frenos, nivel de aceite hidráulico, y otros niveles (lista no recorrida por completo).
- Cada ítem muestra el criterio de no conforme en el texto (p. ej. "NO CONFORME: NIVEL BAJO").

### Pasos 3 y 4
**No explorados todavía** (Evidencia Fotográfica y Resumen).

### Lecciones para el bot
- La lógica es **dinámica por contrato/vehículo** (vehículos disponibles, n.º interno, último odómetro, llantas internas): el bot debe consultar esos datos en el backend (Odoo) y no estar fijo.
- Hay tres patrones de respuesta: casilla simple (marcar = conforme), radio de 3 opciones (incluye "No aplica") y campos numéricos (labrado, PSI).
- El criterio de "no conforme" va incrustado en el texto de cada ítem; conviene guardarlo como dato estructurado.
- Validar odómetro contra el último registrado y conservar ese mensaje de ayuda.
- Pestañas con scroll anidado dificultan el uso en móvil; considerar acordeones o una sección por pantalla.
- Falta confirmar qué ítems "no conforme" bloquean la operación o notifican al jefe.

---

## 8.1 Inventario completo del paso 2 (Verificación de Elementos)

Recorrido completo de las pestañas Equipo y Vehículo. Sigue sin enviarse nada.

### Documentos (7, todas casilla simple)
Licencia de Conducción · Licencia de Tránsito · SOAT · Revisión Tecnomecánica · Extracto de Contrato · Tarjeta de Operación · Cédula de Ciudadanía.

### Rines/Llantas
Casillas "Estado de los rines conforme" y "Estado de las llantas conforme"; Llantas 1–4 (Labrado mm + Presión PSI); botón "El vehículo tiene llantas internas → Sí" (muestra llantas 5 y 6); Llanta de Repuesto (Labrado + Presión).

### Equipo — "EQUIPO DE PREVENCIÓN Y SEGURIDAD" (orden real)
| # | Ítem | Control |
|---|------|---------|
| 1 | Conos (sin existencia, dañado, solo una unidad) | Casilla |
| 2 | Cuñas (sin existencia, dañado, solo una unidad) | Casilla |
| 3 | Botiquín de primeros auxilios | Casilla |
| 4 | Gato | Casilla |
| 5 | Linterna (sin existencia, sin baterías o baterías en mal estado) | Casilla |
| 6 | Herramientas básicas (alicate, destornilladores, llave de expansión y llaves fijas) | Casilla |
| 7 | Chaleco reflectivo | Radio: Conforme / No conforme |
| 8 | Pico y pala | Radio: Conforme / No conforme / No aplica |
| 9 | Eslingas (sin existencia, en mal estado) | Radio de 3 opciones |
| 10 | Kit control derrames químicos | Radio: Conforme / No conforme |
| 11 | Cables de arranque | Radio de 3 opciones |
| 12 | Cámaras de vehículo (delantera y trasera) | Radio de 3 opciones |
| 13 | Estado de barra antivuelco (dañado, con fisuras o defectos) | Radio de 3 opciones |
| 14 | Cruceta (sin existencia, dañado) | Casilla |
| 15 | Profundímetro | Casilla |
| 16 | Medidor de aire | Casilla |
| 17 | **Testigos del tablero** | Radio: Conforme / No conforme |

### Vehículo (agrupado por secciones)
- **EXTINTORES:** Extintor N.º 1 (casilla); Extintor N.º 2 (radio de 3 opciones).
- **CHASIS Y CABINA (casillas):** orden y aseo dentro y fuera del vehículo; cinturones de seguridad; suspensión–dirección; espejos y retrovisores; vidrios; estado de inclinación del espaldar; cojinería del asiento; fijación del asiento.
- **NIVELES, SISTEMAS DE DIRECCIÓN, MOTOR Y FRENOS (casillas):** nivel de líquido de frenos; nivel de aceite hidráulico; nivel de combustible; nivel de líquido refrigerante; nivel aceite de motor; frenos; freno de mano (tensión baja); correas.
- **SISTEMA ELÉCTRICO (casillas):** switche de encendido o starter; pito; pito de reversa; batería; dispositivo de velocidad; cierre de puertas; aire acondicionado; velocímetro; limpiaparabrisas; luces delanteras, traseras, reversa y parada (stop); luces direccionales.

### Observaciones adicionales
- Hay **tres tipos de control**: casilla simple, radio de 2 opciones (Conforme/No conforme) y radio de 3 opciones (con "No aplica"). El tipo depende del ítem, no de la pestaña.
- El texto de cada ítem incluye el criterio de "no conforme" entre paréntesis; la casilla marcada parece significar "conforme" (por confirmar).
- Cada pestaña es una lista larga con scroll interno; en pantalla chica solo se ven ~5 ítems a la vez.
- Los ítems de la pestaña Vehículo y Equipo parecen ser configurables por tipo de vehículo (el banner de "llantas internas" sugiere lógica por vehículo).
- Incidente en la prueba: un clic marcó por error la casilla "Cierre de puertas" y se desmarcó de inmediato; el formulario no se envió.

---

## 9. Preoperacional: pasos 3 y 4 y requisitos de diseño (2026-11-23)

El usuario marcó las opciones del paso 2 (dejando una sin marcar como ejemplo) para llegar hasta el final. Se recorrieron los pasos 3 y 4 **sin pulsar "Completar Inspección"**: nada se envió.

### Paso 3 – Observaciones y Evidencia Fotográfica
- **OBSERVACIONES Y ESTADO FINAL:** textarea libre ("Ingrese cualquier observación adicional sobre el estado del vehículo…").
- **Subir imagen de testigos:** zona punteada "+ Agregar foto" (1 imagen; foto de los testigos del tablero).
- Botones **← Atrás** / **Siguiente →**. "Siguiente" **no exigió la foto** para avanzar: la validación solo se muestra como alerta en el resumen.

### Paso 4 – Resumen de la Inspección (solo lectura, antes de enviar)
Tarjetas con lo capturado en pasos anteriores:
1. **Información General:** Conductor, Contrato, Responsable Jefe, Vehículo (Marca/Modelo/ABC125), Número Interno (090), Odómetro (253600).
2. **Documentos del Vehículo:** los 7 documentos, cada uno con icono ✔ verde.
3. **Rines y Llantas:** estado de rines y llantas (✔); tabla **Labrado / Presión** por llanta (L1–L6, "-" cuando no aplica) y Repuesto. Ejemplo: L1 5/45, L2 6/45, L3 8/40, L4 6/40, Repuesto 8/30.
4. **Equipo de Prevención y Seguridad:** rejilla a dos columnas con ✔ por ítem; los marcados "No aplica" aparecen como **N/A** (Pico y Pala, Extintor N.º 2).
5. **Extintores**, **Chasis y Cabina**, **Niveles, Sistemas, Motor y Frenos**, **Sistema Eléctrico:** misma rejilla con ✔.
6. **Imagen Testigo:** "Imagen subida: 0/1" con icono de advertencia naranja.
- Pie: **← Atrás** (permite volver a corregir) y botón verde **Completar Inspección** (envío final; no pulsado).

### Cómo marca el resumen cada estado
| Estado del campo | Icono en el resumen |
|---|---|
| Marcado / Conforme | ✔ verde |
| **Sin marcar** o **No conforme** | **⚠ círculo rojo con "!"** (alerta). Ejemplo: "Direccionales" quedó sin marcar |
| No aplica | **N/A** en gris |
| Pendiente de evidencia (foto) | **0/1** + advertencia naranja |

Regla indicada por el usuario: los campos no seleccionados o marcados "No conforme" se muestran como alertados en el resumen; el usuario puede devolverse para corregir antes de enviar.

### Requisitos de diseño pedidos por el usuario para el nuevo bot / mini app
1. **Responsive para móviles:** la mini app debe verse y usarse bien en pantallas pequeñas (hoy las listas largas con scroll interno de ~5 ítems y las pestañas obligan a mucho desplazamiento).
2. **Teclado numérico** en los campos de valores (odómetro, labrado, PSI, kilometraje, galones, precios): usar `inputmode="numeric"` (o `decimal` para labrado y precio) para que se despliegue el teclado numérico en el móvil.
3. **Resumen con alertas** como el actual, con posibilidad de volver a cada paso a corregir.

### Recomendaciones técnicas derivadas
- Un solo scroll por pantalla (evitar paneles con scroll interno dentro de otro scroll) y barra de navegación (Atrás/Siguiente) fija abajo.
- Botones/casillas con área táctil mínima de ~44 px; el texto de cada ítem junto con el criterio de "no conforme" en una segunda línea más pequeña.
- Campos numéricos: `inputmode="numeric"`/`"decimal"`, `pattern` y validación en vivo (p. ej. odómetro ≥ último registrado).
- En el resumen, enlazar cada alerta al ítem para saltar directamente a corregirlo, y deshabilitar o confirmar el envío si hay alertas críticas.
- Incluir la evidencia fotográfica como requisito explícito con contador (0/1) y bloqueo opcional del envío.
- Confirmar la política de envío: ¿se permite enviar con ítems sin marcar o "No conforme"? (hoy solo se alertan).

### Decisiones del usuario (2026-11-23)
- **El sistema debe permitir enviar el Preoperacional con ítems alertados** (sin marcar o "No conforme"): las alertas del resumen son informativas, no bloquean el envío. Por tanto la foto de testigos tampoco debe bloquear el envío; solo advertir.
- **Alcance actual:** enfocarse únicamente en el formulario **Preoperacional**; los demás (Prueba de Ruta, AST, Pos-Operacional, Combustible, Gastos) quedan para después.
- El formulario de prueba se cerró sin enviar (se descartó el borrador).

---

## 10. AST: decisiones e implementación (2026-11-25)

Segundo formulario implementado en @OperativosViacoturBot, después del Preoperacional. Detalle técnico en
[`docs/AST.md`](AST.md).

- **Modelo de Odoo:** `ast` ("Analisis Seguro de Trabajo", módulos `preoperacional` y `preoperacional_inherit`).
  Tiene 10 preguntas: las 8 del bot de referencia más **`documentos`** (¿documentos vigentes?) y
  **`descanso`** (¿descansó adecuadamente?). Odoo calcula `state` y `plan_accion`: cualquier "No conforme" o
  medicamentos = "Sí" deja el registro *no conforme* con plan de acción.
- **Decisiones del usuario:**
  1. El AST vive en el mismo bot (botón ⚠️ AST del panel 📋 Formularios y `/ast`).
  2. Se preguntan las **10** preguntas de Odoo, todas obligatorias.
  3. Con "No conforme" o medicamentos = "Sí" **se permite enviar** (con confirmación) y se **avisa al jefe
     inmediato**; con "Sí" el nombre del medicamento es obligatorio. Esto responde la pregunta abierta 7 (§ 6).
  4. El contrato sale de los **extractos aprobados y vigentes** del conductor; sin extracto, pantalla de bloqueo.
- **Diseño:** dos pasos (Verificación → Resumen) con la identidad del preoperacional; botones Conforme /
  No conforme de 44 px; aviso de pendientes que lleva a cada pregunta; resumen con alertas que regresan a la
  pregunta; confirmación al enviar y mensaje en el chat con el número AST-xxxx.
