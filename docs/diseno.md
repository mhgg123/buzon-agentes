# Diseño y alcance

## De dónde viene esto

Este proyecto es la generalización de un canal de coordinación entre dos agentes
de código (Claude Code y Codex) que operó primero en un despliegue privado,
donde acumuló uso real e incidentes reales antes de separarse en un repositorio
propio, sin nada específico de esa instalación original (rutas, alertas,
nombres de equipo, credenciales).

## Qué hace hoy (v1)

- Buzón durable (SQLite): mensajes, hilos, reintentos, claves de tarea
  anti-duplicado, registro de agentes en caliente, arriendos de rutas para que
  dos agentes no editen lo mismo a la vez.
- Despachador configurable: corre un agente registrado con el comando que
  usted defina en `agentes.toml`, sin saber nada de qué CLI es.
- Escritura con revisión cruzada: un agente escribe en un clon aislado de un
  repo git; el diff completo va al otro agente configurado como su revisor;
  solo si aprueba, el commit queda marcado `aprobado_local`.
- Guardas: rechaza ramas `main`/`master`, rutas sensibles (`.github/`, `.ssh/`,
  secretos), objetos no regulares, demasiados archivos. Cualquier guarda que
  frene una escritura **escala a sesiones interactivas** en vez de fallar en
  silencio — ver la nota de diseño abajo.

## Un defecto real que esta generalización encontró y corrigió

Al portar el despachador se encontraron dos funciones completas, escritas y
probadas en aislamiento, pero **nunca conectadas** al flujo real:

1. `rama_actual()` (el rechazo de `main`/`master`) no se llamaba desde ningún
   lugar: una tarea de escritura contra un repo en `main` se aceptaba sin
   objeción.
2. `escalar()` (el aviso a sesiones interactivas) tampoco se llamaba: una
   guarda que fallaba *antes* de que existiera la fila de seguimiento en la
   tabla `escrituras` fallaba sin avisar a nadie.

Las pruebas en `tests/test_despachador.py` (`test_rama_main_se_rechaza_para_escritura`)
cubren exactamente este caso para que no vuelva a pasar inadvertido.

## Qué NO hace (todavía) y por qué

- **No automatiza `git push`.** La aprobación cruzada queda como
  `aprobado_local`: el commit vive en un checkout aislado, listo para que un
  paso posterior (humano o automatizado, fuera de este repo) lo integre y
  corra las pruebas reales del proyecto. Automatizar el push exige resolver
  primero cómo se retiran credenciales de forma confiable (hoy solo se quitan
  variables de entorno conocidas: `SSH_AUTH_SOCK`, `GIT_SSH_COMMAND`, etc. —
  no es una sandbox, es una reducción de superficie) y qué pasa si `HEAD` se
  movió entre la aprobación y el push.
- **No aísla procesos ni red.** Un clon de git no es una sandbox: si el agente
  de escritura tiene acceso de red o puede ejecutar lo que quiera en su propio
  proceso, un clon no lo detiene. Para eso, póngalo detrás de un contenedor o
  una sandbox real (igual que haría con cualquier agente autónomo).
- **Solo pares dirigidos, no N agentes.** `[revision.pares]` asume que cada
  agente de escritura tiene exactamente un revisor fijo. Generalizar a más de
  dos agentes revisándose round-robin es una extensión futura, no algo que
  esta versión resuelva.

## Por qué no usar directamente OpenHands

Ver el README para la comparación completa. En resumen: al momento de escribir
esto, OpenHands no documenta ni una cola durable ni control de concurrencia
nativo, y tiene issues abiertas confirmándolo. Este proyecto no es una
alternativa general a OpenHands — es una pieza chica y enfocada para el caso
puntual de "dos agentes de código, revisión obligatoria antes de cualquier
cambio", que se puede usar sola o como complemento de otra herramienta.
