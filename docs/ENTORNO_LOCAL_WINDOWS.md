# Entorno local de pruebas en Windows 11

Guía para correr **telegram_boarding_bot** (bot de pasajeros, puerto 8010) y
**telegram_bot_operativos** (bot de conductores, puerto 8011) en un PC con Windows 11,
conectados al **Odoo de qa2** y a **GitHub** para hacer commits y PR.

Decisiones (2026-11-24):

- **Odoo:** el de qa2, remoto. No se instala Odoo en Windows.
- **Telegram:** los **mismos bots** de qa2. Un token solo puede estar activo en un lugar
  a la vez, así que **mientras pruebas en Windows los bots de qa2 deben estar apagados**
  (ver § 5). Si se vuelve incómodo, crea bots de prueba en @BotFather y usa sus tokens.

```
Windows 11                                   qa2 (192.168.1.45)
┌───────────────────────────────┐            ┌──────────────────────────────┐
│ telegram_boarding_bot  :8010  │──XML-RPC──▶│ Odoo :8079  (odoo.viacotur.com)│
│ telegram_bot_operativos :8011 │──REST/RPC─▶│  bot.telegram.config         │
│ cloudflared (2 túneles HTTPS) │            │ bots de qa2 APAGADOS         │
└───────────────────────────────┘            └──────────────────────────────┘
        ▲ webhook / Mini Apps
     Telegram
```

> **XAMPP no se usa.** Los bots son Python (FastAPI) con SQLite o PostgreSQL; no usan
> Apache, PHP ni MySQL. Puede seguir instalado: sus puertos (80, 443, 3306) no chocan
> con 8010 y 8011.

## 1. Instalar herramientas

En **PowerShell** (no hace falta administrador para la mayoría):

```powershell
winget install -e --id Python.Python.3.12
winget install -e --id Git.Git
winget install -e --id GitHub.cli
winget install -e --id Cloudflare.cloudflared
```

Cierra y vuelve a abrir PowerShell, y comprueba:

```powershell
py -3.12 --version; git --version; gh --version; cloudflared --version
```

Permite activar entornos virtuales (una sola vez):

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## 2. Conectar con GitHub y clonar

```powershell
gh auth login            # GitHub.com → HTTPS → "Login with a web browser"
gh auth setup-git        # git usa la sesión de gh para push
git config --global user.name  "Tu Nombre"
git config --global user.email "tu-correo@ejemplo.com"

mkdir C:\bots; cd C:\bots
gh repo clone DavidR84Dev/telegram_bot_operativos
gh repo clone DavidR84Dev/telegram_boarding_bot

cd C:\bots\telegram_bot_operativos; git switch test_bot     # rama de calidad
cd C:\bots\telegram_boarding_bot;  git switch qa_bot       # rama de calidad
```

## 3. Entorno de Python

```powershell
cd C:\bots\telegram_bot_operativos
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
pytest -q                 # deben pasar todas
deactivate

cd C:\bots\telegram_boarding_bot
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest -q
deactivate
```

## 4. Archivos `.env`

Los `.env` **no están en GitHub** (tienen contraseñas). Créalos a partir de
`.env.example` y pide los valores reales por un medio seguro (no por chat ni correo
sin cifrar).

```powershell
Copy-Item C:\bots\telegram_bot_operativos\.env.example C:\bots\telegram_bot_operativos\.env
Copy-Item C:\bots\telegram_boarding_bot\.env.example  C:\bots\telegram_boarding_bot\.env
notepad C:\bots\telegram_bot_operativos\.env
```

**Dirección de Odoo (`ODOO_URL` en los dos bots):**

| Dónde está el PC | `ODOO_URL` |
|---|---|
| Fuera de la oficina (o con datos móviles compartidos) | `https://odoo.viacotur.com` |
| En la red de la oficina | `http://192.168.1.45:8079`. Desde la LAN la IP pública no responde (el MikroTik no hace *hairpin NAT*). Si no conecta, en qa2 hay que permitirlo: `sudo ufw allow from 192.168.1.0/24 to any port 8079 proto tcp` |

**telegram_bot_operativos** — diferencias con qa2:

```ini
APP_ENV=development
ODOO_URL=https://odoo.viacotur.com          # o http://192.168.1.45:8079 (ver tabla)
ODOO_DB=qa2
TELEGRAM_MODE=polling                       # no necesita dominio para recibir mensajes
DATABASE_URL=sqlite:///run/watches.sqlite3  # base local; se crea sola
SCANNER_BASE_URL=https://<túnel-8011>.trycloudflare.com   # paso 5.2
WEBAPP_BASE_URL=https://odoo.viacotur.com
BOARDING_BOT_INTERNAL_URL=http://127.0.0.1:8010
LISTEN_HOST=127.0.0.1
LISTEN_PORT=8011
# Mismos valores que qa2: ODOO_USERNAME/PASSWORD, ODOO_API_LOGIN/PASSWORD, TELEGRAM_BOT_TOKEN
```

La base local (SQLite) arranca vacía: al iniciar, el bot sincroniza empleados, flota,
extractos y acuerdos desde Odoo. Las **reservas de vehículos y los preoperacionales
pendientes son independientes** de los de qa2 (cada entorno tiene su base), pero los
preoperacionales que se envíen **sí se crean en el Odoo de qa2**.

**telegram_boarding_bot** — solo lleva la conexión a Odoo; el token, el dominio y los
secretos los lee de `bot.telegram.config` en Odoo:

```ini
APP_ENV=development
ODOO_URL=https://odoo.viacotur.com          # o http://192.168.1.45:8079
ODOO_DB=qa2
ODOO_USERNAME=...
ODOO_PASSWORD=...
LISTEN_HOST=127.0.0.1
LISTEN_PORT=8010
```

## 5. Sesión de pruebas

### 5.1 Apagar los bots de qa2 (en el servidor qa2)

Primero el de abordaje: al apagarse **borra su webhook** en Telegram, y si se apaga
después de encender el de Windows, borraría el webhook del PC.

```bash
sudo systemctl stop telegram-bot                                         # abordaje
cd /opt/odoo/custom/telegram_bot_operativos && docker compose stop app    # operativos
```

### 5.2 Túneles HTTPS en Windows

Telegram necesita HTTPS público para el webhook del bot de abordaje y para los Mini Apps
de operativos. Abre **dos** ventanas de PowerShell:

```powershell
cloudflared tunnel --url http://127.0.0.1:8010    # anota la URL: túnel del abordaje
cloudflared tunnel --url http://127.0.0.1:8011    # anota la URL: túnel de operativos
```

- Copia la URL del túnel de 8011 en `SCANNER_BASE_URL` del `.env` de operativos.
- En **Odoo qa2** → registro activo de `bot.telegram.config`, cambia **Public_Domain_Bot**
  al host del túnel de 8010 **sin** `https://` (p. ej. `abc-def.trycloudflare.com`).
  Anota el valor anterior (`bot.viacotur.com`) para restaurarlo al terminar.

Las URL `*.trycloudflare.com` cambian cada vez que se abre el túnel.

### 5.3 Encender los bots en Windows

```powershell
# Ventana 3: abordaje
cd C:\bots\telegram_boarding_bot; .\.venv\Scripts\Activate.ps1
uvicorn app.main:app --host 127.0.0.1 --port 8010

# Ventana 4: operativos
cd C:\bots\telegram_bot_operativos; .\.venv\Scripts\Activate.ps1
uvicorn app.main:app --host 127.0.0.1 --port 8011
```

En el log deben aparecer `Webhook registrado: https://<túnel-8010>/telegram/webhook`
(abordaje) y `Modo polling activo` (operativos). Si Windows Defender pregunta por Python,
permite solo **redes privadas**.

Con `--reload` uvicorn reinicia solo al guardar cambios en el código (útil mientras programas).

### 5.4 Terminar la sesión (dejar qa2 como estaba)

1. `Ctrl+C` en las ventanas de los bots y de los túneles.
2. En Odoo qa2, devuelve **Public_Domain_Bot** a `bot.viacotur.com`.
3. En qa2:

   ```bash
   sudo systemctl start telegram-bot
   cd /opt/odoo/custom/telegram_bot_operativos && docker compose start app
   ```

4. Comprueba en qa2: `journalctl -u telegram-bot -n 20` (debe decir *Webhook registrado:
   https://bot.viacotur.com/...*) y `docker compose logs --tail 5 app` (*Modo polling activo*).

## 6. Commits y PR desde Windows

Mismo flujo que en qa2: se trabaja en la rama de calidad y se integra a `main` con un PR.

```powershell
cd C:\bots\telegram_bot_operativos
git pull                                   # traer lo último de test_bot
# ... cambios + pytest -q + ruff check app tests ...
git add <archivos>
git commit -m "Describe el cambio"
git push
gh pr create --base main --head test_bot --fill      # si no hay un PR abierto
gh pr view --web
```

En el bot de abordaje la rama de calidad es `qa_bot`.

**Llevar los cambios a qa2** (después del push):

```bash
cd /opt/odoo/custom/telegram_bot_operativos && git pull && docker compose up -d --build
cd /opt/odoo/custom/telegram_boarding_bot  && git pull && sudo systemctl restart telegram-bot
```

Si en qa2 hay cambios sin confirmar, `git pull` puede fallar: revísalos con `git status`
antes de tocar nada.

## 7. Problemas frecuentes

| Síntoma | Causa y solución |
|---|---|
| `Conflict: terminated by other getUpdates request` (operativos) | El bot de qa2 sigue encendido con el mismo token. Apágalo (§ 5.1). |
| El bot de abordaje no recibe mensajes | `Public_Domain_Bot` no apunta al túnel actual, o el bot de qa2 se apagó después del de Windows y borró el webhook. Reinicia el bot de Windows. |
| El Mini App dice que no encuentra el servidor | La URL del túnel cambió: actualiza `SCANNER_BASE_URL`, reinicia el bot y toca **📋 Formularios** para recibir botones nuevos. |
| No conecta con Odoo desde la oficina | Usa `http://192.168.1.45:8079` (ver § 4). |
| `Activate.ps1 cannot be loaded` | Falta `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. |
| `git push` pide usuario y contraseña | Ejecuta `gh auth setup-git`. |
