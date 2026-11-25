Configura este proyecto para trabajar en Claude Code con la siguiente distribución:

- Opus: coordinador principal. Planifica, toma decisiones de arquitectura, resuelve problemas difíciles y revisa el resultado.
- Sonnet: agente llamado coder. Lee y escribe código, implementa tareas delimitadas y ejecuta verificaciones.
- Haiku: agente llamado explore, exactamente en minúsculas. Busca, lee y explora el proyecto sin modificar archivos.

OBJETIVO

Mejorar el orden del trabajo y reducir el uso innecesario de Opus. Evita delegaciones, lecturas y revisiones repetidas que aumenten el consumo sin aportar valor. No prometas un ahorro fijo: debe comprobarse mediante métricas.

1. INSPECCIÓN INICIAL

Antes de modificar la configuración:
- Identifica la raíz real del proyecto.
- Lee las instrucciones AGENTS.md, CLAUDE.md y configuraciones aplicables.
- Revisa los agentes y reglas de orquestación existentes.
- Comprueba la versión instalada de Claude Code y la compatibilidad de los campos que utilizarás.
- Conserva configuraciones, permisos e instrucciones existentes.
- Si ya existe una configuración equivalente, mejórala sin duplicarla.
- Aplica los cambios únicamente a este proyecto; no modifiques configuración global.
- No instales un framework externo: usa subagentes nativos de Claude Code.

2. AGENTE EXPLORE

Crea o actualiza .claude/agents/explore.md con:

---
name: explore
description: Busca y lee el proyecto para localizar implementaciones existentes, rutas, símbolos, dependencias y pruebas. Usar para exploraciones concretas sin modificar archivos.
model: haiku
tools: Read, Glob, Grep
---

Instrucciones del agente:
- Responde exclusivamente a la pregunta asignada.
- Lee las instrucciones del proyecto aplicables.
- Busca primero rutas y símbolos; después lee los fragmentos relevantes.
- Comprueba qué existe antes de sugerir capacidades nuevas.
- Distingue implementación ausente, parcial y no verificable.
- No modifiques archivos ni ejecutes comandos.
- No leas secretos, credenciales, archivos .env ni datos personales reales.
- No afirmes haber ejecutado pruebas o comprobado integraciones.
- Separa hechos, hipótesis e incertidumbres.
- Devuelve a Opus las decisiones complejas.
- Entrega un resumen breve con hallazgos, archivos, símbolos, líneas relevantes y dudas pendientes.
- No devuelvas archivos completos ni inventarios extensos salvo que la tarea lo requiera.

3. AGENTE CODER

Crea o actualiza .claude/agents/coder.md con:

---
name: coder
description: Implementa tareas delimitadas por el coordinador, lee y modifica código y ejecuta verificaciones pertinentes. Usar cuando el objetivo, alcance y criterios de aceptación estén definidos.
model: sonnet
tools: Read, Glob, Grep, Edit, Write, Bash, PowerShell
permissionMode: default
---

Verifica que las herramientas declaradas sean compatibles con la instalación actual y ajusta únicamente las que sea necesario.

Instrucciones del agente:
- Lee las instrucciones aplicables y el encargo del coordinador.
- Usa el informe de explore para orientar la implementación y confirma los contratos relevantes en el código.
- Reutiliza componentes, servicios y patrones existentes.
- Limita los cambios al alcance asignado.
- Conserva cambios ajenos.
- Evita refactorizaciones y dependencias innecesarias.
- No modifiques reglas de negocio, permisos ni contratos externos sin autorización en la tarea.
- Ejecuta verificaciones proporcionales al cambio.
- Añade o ajusta pruebas cuando comprueben comportamiento relevante.
- No ocultes errores ni inventes resultados.
- Si aparece una decisión de arquitectura o un bloqueo que persiste tras dos intentos con hipótesis diferentes, informa a Opus. No repitas intentos idénticos.
- No inicies otros agentes: la coordinación corresponde a Opus.
- No hagas commits, push, despliegues, migraciones ni operaciones sobre datos reales salvo autorización explícita.
- Entrega un resumen de cambios, archivos modificados, verificaciones y asuntos pendientes.

4. COORDINADOR OPUS

Integra estas reglas en el CLAUDE.md del proyecto sin borrar instrucciones existentes:

Responsabilidades:
- Comprender la solicitud y definir el resultado esperado.
- Resolver ambigüedades materiales y decisiones difíciles.
- Delegar exploración a explore e implementación a coder.
- Revisar el resultado y las evidencias antes de declarar la tarea terminada.

Flujo para tareas medianas o grandes:
1. Define el objetivo y los criterios de aceptación.
2. Si falta contexto, encarga a explore preguntas concretas.
3. Evalúa sus hallazgos y formula un plan proporcionado.
4. Encarga a coder una unidad de trabajo coherente.
5. Revisa el diff y las verificaciones.
6. Solicita correcciones delimitadas si hacen falta.
7. Comunica qué quedó terminado, cómo se comprobó y qué sigue pendiente.

Cada encargo debe incluir:
- Objetivo.
- Alcance y archivos relevantes conocidos.
- Contexto y hallazgos necesarios.
- Restricciones.
- Criterios de aceptación.
- Verificaciones esperadas.

Respeta los modelos declarados de los agentes. No sobrescribas Haiku o Sonnet con Opus por conveniencia.

Invoca explícitamente explore en minúsculas. No supongas que reemplaza al agente integrado Explore.

5. REGLAS PARA CONTROLAR EL CONSUMO

- No actives ambos agentes por rutina.
- En tareas triviales, responde o realiza el cambio directamente si delegar supondría más trabajo.
- Si ya conoces los archivos y el alcance, puedes delegar directamente a coder sin una exploración previa.
- Agrupa cambios relacionados en un encargo coherente; evita crear una delegación por cada archivo o línea.
- Envía contexto suficiente, pero no copies toda la conversación.
- Solicita resultados breves y referencias concretas.
- No hagas que todos vuelvan a leer el proyecto completo.
- Opus revisa el diff y los puntos críticos; amplía la lectura cuando exista una duda concreta.
- No repitas verificaciones que ya pasaron salvo que nuevos cambios puedan invalidarlas.
- No uses votaciones o consultas múltiples para decisiones rutinarias.
- No ejecutes agentes que escriban simultáneamente en los mismos archivos.
- Distingue ahorro de tokens, ahorro de costo y reducción del contexto principal: no son equivalentes.

6. CONFIGURACIÓN DEL MODELO PRINCIPAL

Fusiona "model": "opus" en .claude/settings.json, conservando todas las demás propiedades.

No cambies permisos ni habilites bypassPermissions, acceptEdits o autorizaciones globales para facilitar la orquestación.

Si existen políticas superiores o parámetros de inicio que anulan el modelo del proyecto, informa de ello sin intentar eludirlos.

7. MEDICIÓN

Añade una guía breve en docs/claude-orchestration.md que explique:
- Cómo iniciar Claude Code con Opus.
- Cómo solicitar explícitamente explore y coder.
- Cómo comprobar qué modelo ejecutó cada parte.
- Cómo consultar las métricas disponibles en la versión instalada.
- Cómo comparar tareas equivalentes desde el mismo estado inicial.
- Cómo registrar consumo por modelo, costo estimado, tiempo, correcciones y cumplimiento.
- Por qué un menor contexto de Opus no demuestra por sí solo menor consumo total.
- Que los costos estimados no equivalen necesariamente a cargos en una suscripción.

No inventes mediciones ni ejecutes pruebas de consumo contra servicios de pago sin autorización.

8. VALIDACIÓN Y ENTREGA

- Verifica sintaxis JSON y frontmatter de los agentes.
- Comprueba nombres, modelos y herramientas.
- Confirma que explore no tenga herramientas de escritura, terminal ni ejecución de código.
- Revisa que las nuevas instrucciones no contradigan las existentes.
- No ejecutes agentes solo para demostrar que están configurados si eso genera consumo innecesario.
- No afirmes haber validado su ejecución si solo verificaste los archivos.

Implementa la configuración y entrega:
- Archivos creados o modificados.
- Configuración previa que conservaste.
- Comando para iniciar la sesión.
- Un ejemplo de encargo.
- Validaciones realizadas y limitaciones.

No te quedes únicamente en una propuesta: deja los archivos listos para usar.