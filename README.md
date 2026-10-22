# buzón-agentes

> 🌐 **English** — This document is written in Spanish. It's a durable mailbox +
> dispatcher for coordinating AI coding agents (Claude Code, Codex, Gemini CLI or
> similar) so they cross-review each other's commits before anything gets pushed.
> Want a translation? There are plenty of AIs for that — Googlee mijo/a!!!
>
> 🌐 **中文（简体）** — 本文档使用西班牙语撰写。这是一个持久化的邮箱 + 调度系统，
> 用于协调 AI 编程代理（如 Claude Code、Codex 或 Gemini CLI），让它们在推送代码
> 前互相审查对方的提交。想要翻译？现在有很多 AI 可以帮你 — Googlee mijo/a!!!
>
> 🌐 **हिन्दी** — यह दस्तावेज़ स्पैनिश में लिखा गया है। यह AI कोडिंग एजेंटों
> (जैसे Claude Code, Codex या Gemini CLI) के बीच समन्वय के लिए एक स्थायी मेलबॉक्स
> + डिस्पैचर है, जो किसी भी पुश से पहले एक-दूसरे के कमिट की समीक्षा करते हैं।
> अनुवाद चाहिए? इसके लिए ढेरों AI मौजूद हैं — Googlee mijo/a!!!
>
> 🌐 **Español** — Este documento está en español, como corresponde. Si de todas
> formas quieres traducirlo, hay IA's de sobra para eso — Googlee mijo/a!!!
>
> 🌐 **العربية** — هذا المستند مكتوب بالإسبانية. إنه صندوق بريد دائم وموزّع لتنسيق
> وكلاء الذكاء الاصطناعي للبرمجة (مثل Claude Code أو Codex أو Gemini CLI) بحيث
> يراجع كل منهما التزامات الآخر قبل أي push. تريد ترجمة؟ هناك أدوات ذكاء اصطناعي
> كثيرة لذلك — Googlee mijo/a!!!

---

## Qué es esto

Un buzón local y durable (SQLite, sin servidor ni API) para que dos o más agentes
de código por línea de comandos —Claude Code, Codex, Gemini CLI, Aider, o
cualquier otro que acepte un prompt por stdin— se coordinen entre sí: se manden
mensajes, se pidan revisiones, y, si así se configura, **escriban código de forma
autónoma sin que nadie los esté mirando, con la condición de que el OTRO agente
revise el commit antes de que se integre.** Nadie aprueba su propio código.

No depende de ningún proveedor de LLM adicional ni de infraestructura en la nube:
corre en la máquina donde ya viven los agentes, con los mismos binarios y
credenciales que usarías desde la terminal. Requiere **Python 3.11 o superior**
(usa `tomllib` de la librería estándar); sin eso, nada más que instalar.

## Por qué existe

Herramientas como [OpenHands](https://github.com/OpenHands/OpenHands) permiten
correr varios agentes de código (vía ACP) y dispararlos con eventos o un
calendario — pero, al momento de escribir esto, su propio issue tracker confirma
que **no hay cola de tareas durable ni control de concurrencia nativo**
([#18593](https://github.com/OpenHands/OpenHands/issues/18593)), y hay bugs
abiertos sobre persistencia de las automatizaciones tras un reinicio
([#15623](https://github.com/OpenHands/OpenHands/issues/15623),
[#18629](https://github.com/OpenHands/OpenHands/issues/18629)).

Este proyecto no compite con OpenHands ni lo reemplaza: resuelve ese hueco
puntual —cola durable, reintentos acotados, revisión cruzada obligatoria antes de
cualquier push— con el mínimo de piezas posible (un archivo SQLite y dos scripts
de Python sin dependencias externas más allá de la librería estándar).

## Instalación rápida

```bash
git clone <esta URL> buzon-agentes
cd buzon-agentes/buzon_agentes

# 1. registrar los agentes que van a usar el canal
python3 buzon.py agent add claude --kind interactiva --receptor claude
python3 buzon.py agent add codex  --kind interactiva --receptor codex

# 2. decirle al despachador cómo correr cada uno
cp ../ejemplos/agentes.toml.example ~/.config/buzon-agentes/agentes.toml
# editar esa copia con los comandos reales de su instalación

# 3. mandar un mensaje y procesarlo
python3 buzon.py send claude codex --text '¿Puedes revisar este cambio?'
python3 buzon.py inbox codex
```

Para que el despachador corra solo cada pocos segundos, agréguelo a un
`cron`/`systemd timer`/`launchd` propio — ver [`docs/despliegue.md`](docs/despliegue.md).

## Escritura con revisión cruzada

```bash
python3 buzon.py send claude codex --auto --write --repo /ruta/al/repo \
  --tarea nombre-unico --text 'Agrega validación de entrada a parse_config().'
```

El despachador clona el `HEAD` confirmado del repo a un checkout aislado y
separado (no toca el working tree real ni cambios sin commit), corre el agente
en modo escritura ahí, y si produce exactamente un commit limpio —sin tocar
`.github/`, secretos, `.ssh/` ni exceder un tope de archivos— manda el diff
completo al OTRO agente configurado en `[revision.pares]` para que lo apruebe o
pida cambios. **Ningún agente puede aprobar su propio commit** (está impedido
por diseño, no solo por convención).

**Límite honesto de esta versión:** la aprobación queda registrada como
`aprobado_local` — el commit vive en el checkout aislado, listo para que un
humano o un paso posterior lo integre y corra las pruebas del proyecto. Todavía
no automatiza el `git push` al repositorio real; ver
[`docs/diseno.md`](docs/diseno.md) para el alcance completo y lo que falta.

## Qué NO hace (todavía)

- No empuja (`git push`) nada al repositorio de origen de forma automática.
- No hay cola con prioridades ni backpressure más allá del tope diario de
  ejecuciones.
- No aísla procesos ni red: un clon de Git no es una sandbox. Si su agente de
  escritura necesita confinamiento real, póngalo detrás de su propio sandbox
  (contenedor, VM, `bwrap`) antes de darle este modo.
- No está pensado para más de dos agentes revisándose entre sí a la vez (el
  diseño de `[revision.pares]` es por pares dirigidos).

## Licencia

MIT — ver [`LICENSE`](LICENSE).
