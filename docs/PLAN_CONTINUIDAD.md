# Plan de continuidad: preoperacional y menú de @OperativosViacoturBot

> Documento de traspaso, por si se pierde la sesión de trabajo. Estado al **2026-11-23** (parámetros del Preoperacional definidos; ver `docs/PREOPERACIONAL.md` §4 y el CHANGELOG).
> Para retomar con Claude Code: *"Lee `docs/PLAN_CONTINUIDAD.md` y `docs/mapa_viacotur_form.md` y continúa desde la sección 5"*.
> Aquí no hay secretos: todas las credenciales viven solo en `.env`.

---

## 1. Contexto

- **Repositorio:** `/opt/odoo/custom/telegram_bot_operativos` (GitHub `DavidR84Dev/telegram_bot_operativos`). Se trabaja en la rama `test_bot`. El paso a `main` va **siempre por PR con `gh pr create --base main --head test_bot`**, nunca con merge directo.
- **Bot:** **@OperativosViacoturBot** es el punto de entrada de todas las funcionalidades operativas. Cada funcionalidad es una Mini App con su botón en el menú.
- **Odoo de pruebas:** `qa2`, en `127.0.0.1:8079` desde el host y `host.docker.internal:8079` desde el contenedor. Es una base de calidad y no afecta producción.
- **Bot de referencia:** `@viacoturform_bot` ("viacotur-form"), que define cómo deben verse y funcionar los formularios. El mapa completo está en [`docs/mapa_viacotur_form.md`](mapa_viacotur_form.md).
- **Especificación original:** ETL de maestros, Mini App con `initData` HMAC, persistencia local con `sync_status` y worker a Odoo por XML-RPC. Implementada y documentada en [`docs/PREOPERACIONAL.md`](PREOPERACIONAL.md).

## 2. Lo que ya está hecho

| Ítem | Estado |
|---|---|
| Módulo `app/preop/`: modelos, ETL, cliente REST, API, worker, writer XML-RPC | ✅ En `main` (PR #2 fusionado, commit `8a7c676`) |
| Token JWT de `/api/authenticate` renovado antes de vencer a las 24 h (claim `exp`), con respaldo ante 401 | ✅ |
| Credenciales `ODOO_API_LOGIN`/`ODOO_API_PASSWORD` en `.env` | ✅ Configuradas por el usuario |
| ETL real contra qa2 | ✅ 269 vehículos, 12 empleados, 1 extracto, 0 inválidos |
| Prueba de punta a punta en qa2 (Mini App, worker, XML-RPC) | ✅ Creó `PRE-000009912` (id 684): `no conforme` + `plan_accion`; reenvío sin duplicado |
| Menú del bot como registro ordenado `MENU` (`app/telegram_app.py`): 📝 Preoperacional → 📷 Escanear QR, más `/escanear` | ✅ |
| Pruebas | ✅ 99 pasan, ~85 % de cobertura, `ruff` y `mypy` limpios |
| Contenedor reconstruido con estos cambios | ✅ `docker compose up -d --build` |
| Túnel de QA (cloudflared) | ✅ Activo; ver la sección 3 |
| Mapa de `@viacoturform_bot` | ✅ `docs/mapa_viacotur_form.md` (aún sin commit) |

Los campos `responsable.mobile`, `responsable_cliente.mobile` y `vehiculo.id` se van a exponer en el endpoint de `viacotur_api`. El ETL ya los lee en forma anidada y no requiere cambios.

## 3. Estado operativo actual (QA)

- **Contenedores:** `telegram_bot_operativos-app-1` (puerto `127.0.0.1:8011`) y `-db-1` (PostgreSQL 16).
- **cloudflared:** versión 2026.11.1, instalada sin sudo en `~/.local/bin/cloudflared` con el checksum verificado.
- **Túnel de QA:** URL `https://<aleatoria>.trycloudflare.com` → `127.0.0.1:8011`; la URL vigente y el log están en `run/cloudflared.log`. **La URL cambia cada vez que se reinicia el túnel.**
- **Cambios en `.env`** (respaldo previo en `run/.env.bak-*`):
  - `PREOP_BASE_URL=127.0.0.1:8079` quedó **comentada**. No era HTTPS y tumbaba el bot. Esa variable debe ser la URL pública de *este* bot, no la de Odoo. Vacía, el bot usa `SCANNER_BASE_URL`.
  - `SCANNER_BASE_URL` aparece **dos veces**: la primera es `https://operativos.viacotur.com`, puesta por el usuario y aún sin DNS; la última es el túnel. **Gana la última.** Cuando el subdominio funcione, se borra la línea del túnel.
- **Página de abordaje:** `WEBAPP_BASE_URL`, el túnel hacia la página de abordaje de Odoo, **no responde**. El escáner abre, pero el botón "Abrir página de abordaje" no carga. Se puede levantar otro túnel hacia `127.0.0.1:8079` con aprobación del usuario, porque expone qa2 a Internet.
- **Registro de prueba en qa2:** `PRE-000009912`, con la observación "PRUEBA E2E bot Telegram (qa2) - ignorar". Se puede borrar.

### Comandos de recuperación

```bash
cd /opt/odoo/custom/telegram_bot_operativos

# Estado
docker compose ps
curl -s http://127.0.0.1:8011/health
docker compose logs app --since 10m | grep -v getUpdates

# Relanzar el túnel de QA si murió (la URL cambia)
setsid nohup ~/.local/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8011 \
  > run/cloudflared.log 2>&1 < /dev/null & disown
sleep 8; grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" run/cloudflared.log | head -1
# Poner esa URL en la ÚLTIMA línea SCANNER_BASE_URL de .env y recargar:
docker compose up -d

# Pruebas y calidad
.venv/bin/python -m pytest -q && .venv/bin/ruff check app tests scripts && .venv/bin/mypy app

# Operación del preoperacional
docker compose exec app python -m app.preop.etl                  # sincronizar maestros
docker compose exec app python -m app.preop.worker --once        # procesar la cola
docker compose exec app python -m app.preop.worker --requeue-all # reencolar fallidos
```

## 4. Comparación con `@viacoturform_bot`

| Aspecto | viacotur-form | Nuestro bot hoy |
|---|---|---|
| Menú | `/start` → teclado "📋 Formularios" → lista inline de 7 formularios | Teclado con un botón por funcionalidad |
| Preoperacional | Wizard de 4 pasos: Información General → Verificación de Elementos → Evidencia Fotográfica → Resumen | Una sola página con scroll |
| Datos de la persona | Conductor y responsable precargados y bloqueados; banner "Conductor validado con extractos aprobados" | Conductor resuelto por PIN (= ID de Telegram) |
| Contrato | Select obligatorio; "Siguiente" deshabilitado hasta elegir | Select (autoseleccionado si hay uno solo) |
| Vehículo | Asignado o tomado del extracto | El conductor lo busca y lo elige |
| Fotos | Paso de evidencia con cámara | No hay |
| Estilo | Logo VIACOTUR; distinto en cada formulario | Estilo genérico de Telegram |

En Odoo, `preoperacional` solo tiene **un** campo de imagen: `testigos_img` (Binary, `attachment=True`). Cualquier otra foto debe guardarse como `ir.attachment` con `res_model='preoperacional'` y `res_id` del registro.

## 5. Decisiones del usuario (resueltas el 2026-11-20: 1A + ubicación, 2A, 3B, 4A; implementadas)

Responder, por ejemplo, "1A, 2A, 3A, 4A". La opción **A** es la recomendada en cada caso.

1. **Menú de @OperativosViacoturBot**
   - **A:** como viacotur-form: teclado `📋 Formularios` + `📷 Escanear QR`; Formularios abre la lista inline con ✅ Preoperacional primero.
   - **B:** mantener el actual.
2. **Formato del preoperacional**
   - **A:** wizard de 4 pasos con barra de progreso.
   - **B:** una sola página.
3. **Fotos**
   - **A:** foto de testigos (`testigos_img`) más fotos de evidencia como adjuntos.
   - **B:** solo testigos.
   - **C:** sin fotos por ahora.
4. **Vehículo**
   - **A:** tomado del extracto aprobado y bloqueado; si hay varios extractos, se elige uno; si no hay, se bloquea con un mensaje claro.
   - **B:** sugerido del extracto, pero editable.
   - **C:** selección libre, como hoy.

Aplicar siempre, sin preguntar: identidad visual VIACOTUR (logo y paleta azul/cian) uniforme en todas las Mini Apps, y las mejoras del punto 5 del mapa (mensajes claros, confirmación al enviar, comandos útiles).

## 6. Paso a paso de la siguiente fase

Este plan asume las opciones **A**; si el usuario elige otra, se ajusta la fase correspondiente. Trabajar en `test_bot`, con pruebas en cada fase.

### Fase 0: preparación
1. `git checkout test_bot && git pull origin main` (el PR #2 ya está en `main`).
2. Hacer commit de `docs/mapa_viacotur_form.md` y de este documento.
3. Verificar el estado con los comandos de la sección 3.

### Fase 1: menú con submenú "📋 Formularios" (decisión 1)
1. En `app/telegram_app.py`, separar el **teclado principal** (`📋 Formularios`, `📷 Escanear QR`) del **catálogo de formularios** (tupla `FORMULARIOS` de `MenuOption`, con ✅ Preoperacional primero).
2. Al pulsar `📋 Formularios`, el bot responde "📂 Formularios disponibles:" con botones **inline `web_app`**, uno por formulario. Desde un botón de teclado las Mini Apps llegan sin `initData`.
3. Comandos: `/start` (menú), `/formularios`, `/preoperacional`, `/escanear`, `/ayuda`.
4. Pruebas en `tests/test_handlers.py`: orden del teclado, lista inline y autorización.
5. Actualizar README y CONTRIBUTING, en la sección "Agregar una opción al menú".

### Fase 2: wizard del preoperacional (decisión 2) e identidad visual
1. Obtener el logo de la compañía (`res.company.logo`, por XML-RPC de solo lectura) y guardarlo en `app/static/`.
2. Rehacer `app/static/preoperacional.html` como wizard, con stepper `1 Información → 2 Verificación → 3 Evidencia → 4 Resumen`:
   - **Paso 1:** conductor y responsable en solo lectura, banner "Conductor validado con extractos aprobados", vehículo según la decisión 4, contrato obligatorio y kilometraje. "Siguiente" queda deshabilitado hasta completar.
   - **Paso 2:** secciones del checklist (`app/preop/checklist.py`), una por pantalla o en acordeón, con "Marcar todo conforme" y medidas de llantas.
   - **Paso 3:** fotos (fase 3).
   - **Paso 4:** resumen con conteo de no conformes, observaciones y botón **Enviar**. Pedir confirmación al cerrar con cambios sin guardar.
3. Mantener el contrato de la API: `POST /api/preop` con `uuid` generado en el cliente para la idempotencia.
4. Usar `textContent` (no `innerHTML`) para mostrar cualquier dato.

### Fase 3: fotos (decisión 3)
1. En el cliente, comprimir con canvas a JPEG de máximo 1600 px y unos 300 KB antes de subir.
2. Nuevo endpoint `POST /api/preop/{uuid}/fotos`, multipart y autenticado con `initData`. Debe:
   - validar el tipo real (cabecera JPEG/PNG), el tamaño máximo y el número de fotos;
   - guardar en la BD local (tabla `preop_fotos`: `preop_id`, `tipo` = testigos/evidencia, `contenido` bytea, `sha256`) o en un volumen `run/preop_media`.
3. El formulario se envía cuando todas las fotos obligatorias están subidas.
4. Worker (`OdooWriter`):
   - `testigos_img` va en los `vals` del `create`;
   - las demás fotos van como `ir.attachment` (`res_model='preoperacional'`, `res_id`), con nombre determinista `preop_<uuid>_<n>.jpg` para no duplicar en los reintentos (buscar antes de crear);
   - verificar el permiso del usuario técnico para crear `ir.attachment`.
5. Pruebas: tamaño y tipo inválidos, subida idempotente y reintento sin duplicar adjuntos.

### Fase 4: vehículo desde el extracto (decisión 4)
1. Hay que relacionar al conductor con el extracto. Hoy `/api/extractos` entrega el conductor **por nombre**; `driver_id` es un partner. Opciones:
   - **a)** Pedir que `viacotur_api` exponga `conductor: {id, name}` (partner) y, en `/api/empleados`, el partner del empleado (`work_contact_id`), igual que con `vehiculo.id`.
   - **b)** Mientras tanto, emparejar por nombre normalizado, usando el extracto solo cuando el emparejamiento sea único.
2. `GET /api/preop/context` devuelve solo los extractos **aprobados** y vigentes del conductor, con su vehículo y contratos.
3. En la Mini App:
   - 1 extracto: vehículo bloqueado;
   - varios: el conductor elige uno;
   - ninguno: pantalla de bloqueo con "Pide a tu responsable que apruebe tu extracto".
4. `service.submit` valida en el servidor que el vehículo corresponda a un extracto aprobado del conductor.

### Fase 5: cierre
1. Pruebas, `ruff` y `mypy` en verde.
2. Prueba en qa2 desde un móvil real, por el túnel.
3. Actualizar `docs/PREOPERACIONAL.md`, README y CHANGELOG.
4. Commit en `test_bot` → `gh pr create --base main --head test_bot` → `docker compose up -d --build` tras el merge.

## 7. Pendientes fuera del código

- [ ] DNS y certificado de `operativos.viacotur.com`, más el bloque nginx hacia `127.0.0.1:8011` (requiere sudo). Luego quitar la línea del túnel en `.env`.
- [ ] Túnel o dominio para `WEBAPP_BASE_URL` (página de abordaje de Odoo), con aprobación del usuario.
- [ ] Cargar el **ID de Telegram en el campo PIN** del empleado (`hr.employee.pin`) de cada conductor **y de cada jefe** (para el aviso de hallazgos). En qa2 solo 4 empleados tienen PIN.
- [ ] `viacotur_api` `/api/acuerdos` ignora el parámetro `company_id` (siempre filtra la compañía 1): corregirlo si hay contratos de otras compañías.
- [ ] Llenar `labrado_camioneta` y `labrado_buses` en cada acuerdo; con 0 el bot usa el mínimo de Odoo (1,6 / 2,0 mm).
- [ ] `viacotur_api`: exponer `responsable.mobile`, `responsable_cliente.mobile`, `vehiculo.id` y, para la fase 4, el id del conductor.
- [ ] Revisar por qué `/api/empleados` devuelve solo 12 empleados en qa2.
- [ ] Borrar el registro de prueba `PRE-000009912` en qa2 (opcional).
- [ ] Preguntas abiertas del mapa: qué hace "🔴 Finalizar", qué pasa tras enviar, y otros formularios (AST, Pos-Operacional, Combustible, Gastos) para fases futuras.
