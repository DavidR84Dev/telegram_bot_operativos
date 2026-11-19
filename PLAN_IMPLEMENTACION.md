# PLAN DE IMPLEMENTACIÓN — Telegram QR Validator (escáner nativo en Mini App, solo consulta)

## Flujo

1. El validador pulsa **📷 Escanear QR** (botón de teclado `web_app`).
2. Se abre la Mini App `SCANNER_BASE_URL/scanner`.
3. La página llama a `Telegram.WebApp.showScanQrPopup()` (escáner nativo de Telegram).
4. La página envía el texto del QR a `POST /api/scan` con `initData` firmado por Telegram.
5. El servidor valida la firma (HMAC del token del bot, antigüedad ≤ 5 min), comprueba la whitelist
   (`hr.employee.pin` = id de Telegram, activo), consulta el ticket en Odoo (solo lectura) y devuelve la ficha.
6. La ficha se muestra en la Mini App con **Abrir página de abordaje** (`WEBAPP_BASE_URL/form/accept/<token>`).
   La página de Odoo confirma el abordaje.
7. Si el ticket es confirmable, se vigila cada 15 s. Al registrarse `abordado1`/`abordado2` en Odoo,
   el bot avisa en el chat del validador. El sondeo solo corre mientras haya vigilancias.

## Decisiones

| Tema | Decisión |
|---|---|
| Lectura del QR | Escáner nativo de Telegram (`showScanQrPopup`). **Sin fotos** ni OpenCV |
| Identidad | `initData` validado en servidor; nunca se confía en el cuerpo de la petición |
| Autorización | `hr.employee.pin` = id de Telegram y `active = True`. Caché 300 s; negativos 60 s |
| Host del QR | Opción B: se usa solo el path `/form/accept/<token>` y `WEBAPP_BASE_URL` |
| Odoo | XML-RPC solo lectura (`search_read`). Sin `write`/`call` (prueba `test_odoo_client_readonly`) |
| Módulo `programacion_operativos` | No se modifica |
| Dispositivos | Móviles con Telegram actualizado (`showScanQrPopup`, Bot API 6.4+) |
| Configuración | Solo `.env` |

## Estado

- [x] Eliminada la lectura por foto (`qr_reader.py`, handler de fotos, OpenCV en `requirements.txt`)
- [x] `app/webapp_auth.py`: validación de `initData` (HMAC-SHA256)
- [x] `app/scan_service.py`: lógica común de escaneo (chat y Mini App)
- [x] `app/web.py`: `GET /scanner` y `POST /api/scan` (401 sin firma válida, 403 fuera de whitelist)
- [x] `app/static/scanner.html`: escáner nativo, ficha, botón de Odoo (textContent, sin HTML inyectado)
- [x] `app/telegram_app.py`: botón de teclado `web_app`; texto con URL como respaldo; sin fotos
- [x] `pytest -q`: **53 pruebas en verde**
- [x] Túnel del escáner `SCANNER_BASE_URL` → `127.0.0.1:8011` (`bin/cloudflared`)
- [x] Verificación real contra qa2 por túnel: autorizado 200 con ficha y vigilancia; no autorizado 403; QR inválido controlado
- [x] Confirmación del abordaje por la página de Odoo → `abordado1` y vigilancia eliminada (aviso enviado)
- [ ] Prueba manual en el móvil del validador: escáner nativo, ficha, botón de Odoo dentro de Telegram y recepción aviso

## Pendientes y riesgos

1. **Cookies / sesión de Odoo en el webview**: la página de Odoo se abre por navegación dentro de la Mini App.
   No se embebe en iframe porque el origen del escáner es distinto del de Odoo (cookies de terceros).
   Confirmar en el móvil que el botón Confirmar funciona.
2. **Volver desde la página de Odoo**: al navegar fuera de la página del escáner, el botón de volver de Telegram
   puede no regresar a la ficha. Se puede cerrar la Mini App y volver a escanear.
3. **Túneles rápidos**: las URL `*.trycloudflare.com` cambian al reiniciar `cloudflared`. Actualizar
   `WEBAPP_BASE_URL` y `SCANNER_BASE_URL` y reiniciar el validador. En producción usar dominios fijos.
4. **Usuario de Odoo**: en pruebas se usa `lider.ti` (admin). Para producción, usuario técnico dedicado con
   `hr.group_hr_user`.
5. **Versión del cliente**: un cliente sin `showScanQrPopup` muestra un aviso para actualizar Telegram.

## Producción (cuando se indique)

- Actualizar `programacion_operativos` en Odoo.sh (hoy `www.viacotur.com/form/accept/...` responde 404).
- Subdominio fijo en el mismo VPS (p. ej. `validador.viacotur.com`), puerto 8011, `TELEGRAM_MODE=webhook`,
  `TELEGRAM_WEBHOOK_SECRET`, certificado y unidad systemd propios.
- `WEBAPP_BASE_URL` y `SCANNER_BASE_URL` con dominios HTTPS fijos.
- Repetir la comprobación de cabeceras de `/form/accept/<token>` con un ticket real.

## Ejecutar (pruebas)

```bash
cd /opt/odoo/custom/telegram_bot_operativos
.venv/bin/python -m pytest -q
setsid nohup .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8011 < /dev/null > run/validator.log 2>&1 &
# Túnel del escáner (ya activo; la URL cambia al reiniciarlo):
setsid nohup /opt/odoo/custom/telegram_boarding_bot/bin/cloudflared tunnel --no-autoupdate \
  --url http://127.0.0.1:8011 < /dev/null > run/cloudflared_scanner.log 2>&1 &
```
