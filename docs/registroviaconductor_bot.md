# Documentación del bot `@registroviaconductor_bot`

> Revisión hecha el 2026-11-24 desde Telegram Web, **solo con lo visible** sin enviar datos al bot.
> Lo observado va en la sección 1. Lo que no pude ver está marcado como **[POR CONFIRMAR]**, y las secciones 3 en adelante son una propuesta de diseño, no una descripción del bot original.

---

## 1. Lo observado

| Elemento | Valor |
|---|---|
| Nombre mostrado | `registro_viaconductor` |
| Username | `@registroviaconductor_bot` |
| Tipo | Bot de Telegram, sin foto de perfil (avatar con la letra "R") |
| Propósito aparente | Registro de conductores (nombre del flujo: "Iniciar registro de conductor") |
| Cuenta relacionada | Otros bots de la misma organización en la lista de chats (Viactour): `viactour_admin`, `viactour-form`, `Operativos-Viacotur`, `Tickets_Reserva` |

### Menú de comandos (registrado en BotFather con `/setcommands`)

| Comando | Descripción |
|---|---|
| `/start` | Iniciar registro de conductor |
| `/cancel` | Cancelar registro actual |

### Conversación observada

```
Usuario: /start
Bot:     ¡Hola! Para registrarte necesito tu nombre completo:
```

- El bot responde al primer `/start` con un saludo y pide **un solo dato por mensaje**, empezando por el nombre completo.
- Tono: español informal, tuteo, signos de apertura (¡ ?), sin emojis en este primer mensaje.
- En un segundo `/start` (3 días después) el chat no muestra respuesta nueva. **[POR CONFIRMAR]** si el bot ignora `/start` mientras hay un registro abierto, si no respondió, o si la sesión expiró.
- El bot no muestra botones inline ni teclado de respuesta en ese primer paso.
- El perfil tiene enlaces a *Bot Privacy Policy*, *Add to Group* y *Report* (opciones estándar de Telegram, no necesariamente configuradas por el desarrollador).

### Lo que NO se pudo observar

- Los pasos posteriores al nombre (campos pedidos, orden, validaciones).
- Si pide documentos o fotos (licencia, cédula, SOAT, foto del vehículo).
- Mensaje de confirmación final y a dónde se guardan los datos (hoja de cálculo, base de datos, API).
- Si hay notificación a un administrador.
- Texto de la descripción del bot (pantalla "What can this bot do?") y la política de privacidad.

---

## 2. Cómo completar la revisión (opcional)

Para documentar el flujo completo, avanzar el registro con **datos ficticios** y cerrar con `/cancel` antes de la confirmación final. Anotar por cada paso: texto exacto del bot, tipo de entrada esperada (texto, foto, contacto, ubicación, botón), mensaje de error al enviar un dato inválido y mensaje final. Pedir confirmación antes de hacerlo si el bot escribe en un sistema real de la empresa.

---

## 3. Diseño propuesto para un bot similar

### 3.1 Flujo (máquina de estados)

```
/start ──► NOMBRE ──► DOCUMENTO ──► TELEFONO ──► [LICENCIA] ──► [VEHICULO/PLACA] ──► [FOTOS] ──► CONFIRMAR ──► GUARDAR
   ▲                                                                                              │
   └────────────────────────────── /cancel (en cualquier estado) ◄────────────────────────────────┘
```

Los campos entre corchetes son típicos de un registro de conductores; ajustarlos a lo que realmente requiera el negocio.

Reglas:
1. Un dato por mensaje, siempre con un texto que diga qué se espera.
2. Validar cada respuesta y repetir la pregunta con un mensaje de error claro si no es válida.
3. `/cancel` funciona en cualquier estado y limpia los datos temporales.
4. `/start` con un registro en curso: preguntar si reiniciar o continuar.
5. Antes de guardar, mostrar un resumen y pedir confirmación (botones Sí / Corregir).
6. Evitar registros duplicados: buscar por número de documento o `telegram_user_id`.

### 3.2 Mensajes de ejemplo (español, mismo tono)

| Estado | Mensaje |
|---|---|
| Inicio | ¡Hola! Para registrarte necesito tu nombre completo: |
| Documento | Gracias, {nombre}. Ahora tu número de documento (solo números): |
| Error documento | Ese documento no parece válido. Envíalo solo con números, sin puntos ni espacios. |
| Teléfono | Comparte tu número de celular o escríbelo con indicativo: |
| Resumen | Revisa tus datos: … ¿Es correcto? |
| Final | ✅ Registro completado. Un administrador revisará tu información. |
| Cancelar | Registro cancelado. Cuando quieras empezar de nuevo escribe /start. |

### 3.3 Datos a guardar (esquema sugerido)

| Campo | Tipo | Nota |
|---|---|---|
| `telegram_user_id` | bigint | Identificador único del usuario, clave anti-duplicados |
| `telegram_username` | text, nulo | Puede no existir |
| `nombre_completo` | text | |
| `documento` | text | Guardar como texto, no número |
| `telefono` | text | |
| `estado` | enum | `pendiente`, `aprobado`, `rechazado` |
| `creado_en` | timestamp | |
| `datos_extra` | json | Campos opcionales según el negocio |

---

## 4. Arquitectura recomendada

- **Lenguaje y librería:** Python con `python-telegram-bot` (v20+, `ConversationHandler`) o `aiogram` 3 (FSM). Alternativa en Node: `grammY` o `Telegraf`.
- **Modo de ejecución:** webhook en producción (un servicio pequeño en Railway, Render, Fly.io o un VPS) y long polling para desarrollo.
- **Almacenamiento:** PostgreSQL o SQLite para empezar; Google Sheets si el equipo administrativo necesita ver los registros sin herramientas adicionales.
- **Estado de la conversación:** en memoria para pruebas, y persistido (Redis o base de datos) en producción para que un reinicio no pierda registros a medias.
- **Notificación a administradores:** enviar un mensaje a un chat o grupo de administración con el resumen y botones inline Aprobar / Rechazar.
- **Configuración:** token del bot y credenciales en variables de entorno, nunca en el código.

### Estructura de proyecto

```
bot/
├── main.py              # arranque, registro de handlers
├── config.py            # lectura de variables de entorno
├── handlers/
│   ├── registro.py      # ConversationHandler / FSM
│   └── admin.py         # aprobación y rechazo
├── validators.py        # nombre, documento, teléfono, placa
├── db.py                # acceso a datos
├── messages.py          # todos los textos en un solo lugar
└── requirements.txt
```

### Esqueleto mínimo (python-telegram-bot v20+)

```python
import os
from telegram import Update
from telegram.ext import (Application, CommandHandler, ConversationHandler,
                          MessageHandler, ContextTypes, filters)

NOMBRE, DOCUMENTO = range(2)

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("¡Hola! Para registrarte necesito tu nombre completo:")
    return NOMBRE

async def nombre(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["nombre"] = update.message.text.strip()
    await update.message.reply_text("Gracias. Ahora tu número de documento (solo números):")
    return DOCUMENTO

async def documento(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if not texto.isdigit():
        await update.message.reply_text("Envíalo solo con números, sin puntos ni espacios.")
        return DOCUMENTO
    ctx.user_data["documento"] = texto
    # guardar en base de datos aquí
    await update.message.reply_text("✅ Registro completado.")
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Registro cancelado. Escribe /start para empezar de nuevo.")
    return ConversationHandler.END

def main():
    app = Application.builder().token(os.environ["BOT_TOKEN"]).build()
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            NOMBRE: [MessageHandler(filters.TEXT & ~filters.COMMAND, nombre)],
            DOCUMENTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, documento)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))
    app.run_polling()

if __name__ == "__main__":
    main()
```

---

## 5. Configuración en BotFather

1. `/newbot` → nombre (`registro_viaconductor`) y username terminado en `bot`.
2. `/setcommands` →
   ```
   start - Iniciar registro de conductor
   cancel - Cancelar registro actual
   ```
3. `/setdescription` → texto de "What can this bot do?" explicando para qué sirve el bot.
4. `/setabouttext` y `/setuserpic` → texto breve del perfil y logo.
5. Política de privacidad: publicar una URL y enlazarla (el bot original muestra el enlace estándar *Bot Privacy Policy*).

---

## 6. Privacidad y seguridad

- Los datos de conductores (documento, teléfono, licencia) son personales: pedir consentimiento explícito antes del primer campo, informar la finalidad y limitar el acceso.
- Si se piden fotos de documentos, guardarlas en almacenamiento privado y no reenviarlas a grupos abiertos.
- Limitar la tasa de mensajes por usuario para evitar abuso.
- Restringir los comandos de administración a una lista de `telegram_user_id` autorizados.

---

## 7. Lista de verificación

- [ ] Flujo completo documentado paso a paso con el bot original (sección 2)
- [ ] Campos definidos y validaciones escritas
- [ ] Esquema de base de datos creado
- [ ] Bot creado en BotFather y comandos configurados
- [ ] `/cancel` probado en cada estado
- [ ] Manejo de duplicados y reinicios probado
- [ ] Aviso de privacidad y consentimiento incluidos
- [ ] Despliegue con webhook y variables de entorno
