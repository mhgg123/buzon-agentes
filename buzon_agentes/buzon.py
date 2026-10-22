#!/usr/bin/env python3
"""Buzón local y durable para coordinar agentes de código de línea de comandos."""

import argparse
import json
import os
import re
import runpy
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# El canal no trae agentes de fabrica: el primer paso de cualquier instalacion es
# `buzon agent add <nombre> --kind interactiva` para cada agente que vaya a usarlo.
# El registro vive en la tabla `agents` y se amplia en caliente, nunca en el codigo.
AGENTES_INICIALES = ()
# interactiva = la lleva una sesion con una persona detras.
# automatica  = la lleva un receptor sin persona; sus respuestas van marcadas.
# servicio    = proceso sin conversacion (un worker, un coordinador).
CLASES_DE_AGENTE = ("interactiva", "automatica", "servicio")
# Quien se espera que trate el mensaje. Es lo que evita que una instancia automatica
# conteste lo que le toca a una sesion interactiva, y al reves.
DESTINATARIOS = ("interactiva", "automatica", "cualquiera")
ORIGENES = ("interactiva", "automatica")
NOMBRE_DE_AGENTE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
DEFAULT_DB = Path(
    os.environ.get("XDG_STATE_HOME", "~/.local/state")
).expanduser() / "buzon-agentes" / "mail.sqlite3"
MAX_BODY = 20_000


def database_path():
    return Path(os.environ.get("BUZON_DB", DEFAULT_DB)).expanduser()


def agregar_columna(db, tabla, columna, tipo):
    """ALTER TABLE ADD COLUMN idempotente y a prueba de carreras.

    Chequear `columnas()` y despues hacer el ALTER es un test-and-set sin candado: dos
    procesos inicializando a la vez pasan los dos el chequeo y el segundo muere con
    `duplicate column name`. Lo encontro una prueba de `wait` que corre en paralelo con
    un `send`. En vez de tomar un candado para una operacion idempotente, se intenta y
    se tolera el choque: el resultado es el mismo y no hay ventana. El `receiver` tenia
    este mismo defecto latente desde que se agrego.
    """
    if columna in columnas(db, tabla):
        return
    try:
        db.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {tipo}")
    except sqlite3.OperationalError as error:
        if "duplicate column" not in str(error).lower():
            raise


def initialize(db):
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 10000")
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = FULL")
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS agents (
            name TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN {CLASES_DE_AGENTE}),
            enabled INTEGER NOT NULL DEFAULT 1,
            added_at TEXT NOT NULL,
            note TEXT,
            receiver TEXT
        )
    """)
    # `receiver` dice con que adaptador puede atenderlo el receptor automatico. Vacio
    # significa que NO tiene uno, y entonces --auto hacia ese agente se rechaza en vez
    # de encolar un trabajo que iba a fallar tres veces antes de volver al inbox.
    agregar_columna(db, "agents", "receiver", "TEXT")
    # `last_inbox_at` es la ultima vez que ese agente MIRO su bandeja. El buzon es
    # durable, pero no sabia si del otro lado habia alguien: un pedido que el receptor
    # automatico no puede atender —revisar un PR, por ejemplo— se queda parado en
    # silencio contra una sesion inactiva, y el que escribe se enters horas despues.
    # Con esto, al enviar se avisa; no se bloquea, porque dejar el mensaje escrito sigue
    # siendo lo correcto.
    agregar_columna(db, "agents", "last_inbox_at", "TEXT")
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            ack_at TEXT,
            reply_to INTEGER REFERENCES messages(id),
            origin TEXT NOT NULL DEFAULT 'interactiva' CHECK(origin IN {ORIGENES}),
            handler TEXT NOT NULL DEFAULT 'cualquiera' CHECK(handler IN {DESTINATARIOS}),
            thread INTEGER,
            task_key TEXT,
            CHECK(sender != recipient)
        )
    """)
    migrate(db)
    # La migración de mensajes maneja su propia transacción. A partir de aquí,
    # serializar la creación y migración de escrituras hasta el commit de initialize.
    db.execute("BEGIN IMMEDIATE")
    db.execute("CREATE INDEX IF NOT EXISTS pending ON messages(recipient, ack_at, id)")
    db.execute("CREATE INDEX IF NOT EXISTS por_hilo ON messages(thread, id)")
    # El hilo lo pone la base, no el que escribe. Asi vale para CUALQUIER escritor,
    # incluido el codigo viejo durante un despliegue a medias y cualquier guion que
    # inserte por SQL directo: comprobado que un `send` de la version anterior contra
    # este esquema dejaba el hilo en NULL.
    db.execute("""
        CREATE TRIGGER IF NOT EXISTS hilo_por_defecto AFTER INSERT ON messages
        WHEN NEW.thread IS NULL
        BEGIN
            UPDATE messages SET thread = COALESCE(
                (SELECT thread FROM messages WHERE id = NEW.reply_to), NEW.reply_to, NEW.id
            ) WHERE id = NEW.id;
        END
    """)
    # Una tarea abierta no se pide dos veces: si ya hay un mensaje sin acusar con la
    # misma clave, el segundo envio falla en vez de duplicar el trabajo.
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS tarea_abierta ON messages(task_key) "
               "WHERE task_key IS NOT NULL AND ack_at IS NULL")
    # Retirar es el emisor desdiciendose de lo que escribio, antes de que el otro lo
    # haya tratado. Faltaba, y se noto: el aviso #177 quedo en la bandeja de codex
    # diciendo algo que yo mismo corregi en el #178, entre diez pendientes. Un mensaje
    # superado que sigue pendiente es una invitacion a trabajar de mas o a contestar lo
    # que no corresponde. No se borra: queda con su motivo y se ve con --all.
    # Va en initialize y no en migrate: `migrate` se va temprano cuando la base ya esta
    # al dia, asi que una base NUEVA nunca habria recibido estas columnas. Y debajo del
    # CREATE TABLE, porque un ALTER sin tabla rompe el buzon entero.
    agregar_columna(db, "messages", "withdrawn_at", "TEXT")
    agregar_columna(db, "messages", "withdrawn_reason", "TEXT")
    # `modo` separa un --auto de siempre (lectura, como hasta ahora) de uno que pide
    # escritura con revision cruzada (FR-017). Sin CHECK a nivel de base para no repetir
    # la reconstruccion de tabla de `migrate()`; se valida en Python antes de insertar.
    agregar_columna(db, "messages", "modo", "TEXT NOT NULL DEFAULT 'lectura'")
    # Solo lo usan los mensajes en modo escritura: la ruta del repo donde se escribe.
    agregar_columna(db, "messages", "repo", "TEXT")
    for nombre, clase in AGENTES_INICIALES:
        db.execute("INSERT OR IGNORE INTO agents(name, kind, added_at, note, receiver) VALUES (?, ?, ?, ?, ?)",
                   (nombre, clase, now(), "agente inicial del canal", nombre))
    db.execute("""
        CREATE TABLE IF NOT EXISTS dispatches (
            message_id INTEGER PRIMARY KEY REFERENCES messages(id),
            turns_left INTEGER NOT NULL CHECK(turns_left BETWEEN 1 AND 2),
            state TEXT NOT NULL CHECK(state IN ('pending', 'running', 'done', 'failed')),
            attempts INTEGER NOT NULL DEFAULT 0,
            started_at REAL,
            next_at REAL NOT NULL DEFAULT 0,
            last_error TEXT,
            finished_at TEXT
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS dispatch_queue ON dispatches(state, next_at, message_id)")
    # Arriendos de rutas: declaran quien edita que, para que dos agentes no toquen el
    # mismo archivo a la vez. No conceden permisos ni confinan nada; son coordinacion.
    db.execute("""
        CREATE TABLE IF NOT EXISTS leases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo TEXT NOT NULL,
            path TEXT NOT NULL,
            owner TEXT NOT NULL,
            message_id INTEGER REFERENCES messages(id),
            note TEXT,
            created_at TEXT NOT NULL,
            expires_at REAL NOT NULL,
            released_at TEXT
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS lease_live ON leases(repo, released_at, expires_at)")
    # Ciclo de vida de una tarea de escritura con revision cruzada (FR-017 a FR-024):
    # nunca se pushea sin que el OTRO agente la haya aprobado. Tabla nueva en vez de
    # ampliar `dispatches`: su CHECK de `state` ya exige reconstruir la tabla para
    # agregar valores, y esto es un ciclo de vida distinto, no otro estado de reparto.
    db.execute("""
        CREATE TABLE IF NOT EXISTS escrituras (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tarea_key TEXT,
            write_message_id INTEGER NOT NULL REFERENCES messages(id),
            review_message_id INTEGER REFERENCES messages(id),
            repo TEXT NOT NULL,
            branch TEXT NOT NULL,
            commit_sha TEXT,
            reviewer TEXT NOT NULL,
            estado TEXT NOT NULL CHECK(estado IN (
                'escribiendo', 'esperando_revision', 'empujado', 'aprobado_local', 'cambios_pedidos',
                'escalado', 'fallido'
            )),
            intentos_revision INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            resolved_at TEXT
        )
    """)
    # La base existente puede tener el CHECK anterior sin aprobado_local.
    ddl = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='escrituras'").fetchone()[0]
    if "aprobado_local" not in ddl:
        db.execute("ALTER TABLE escrituras RENAME TO escrituras_anteriores")
        db.execute(ddl.replace("'empujado',", "'empujado', 'aprobado_local',"))
        db.execute("INSERT INTO escrituras SELECT * FROM escrituras_anteriores")
        db.execute("DROP TABLE escrituras_anteriores")
    db.execute("CREATE INDEX IF NOT EXISTS escrituras_por_revision ON escrituras(review_message_id)")
    db.execute("CREATE INDEX IF NOT EXISTS escrituras_por_write ON escrituras(write_message_id)")
    db.commit()


def columnas(db, tabla):
    return {fila[1] for fila in db.execute(f"PRAGMA table_info({tabla})")}


def migrate(db):
    """Lleva una base vieja al esquema nuevo sin perder un mensaje.

    Dos casos distintos. Si la tabla trae el CHECK antiguo que fijaba los nombres de
    agente, no hay ALTER que lo quite: hay que reconstruirla. Si solo faltan las
    columnas nuevas, basta agregarlas.
    """
    definicion = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone()
    if definicion is None:
        return
    cerrada = "CHECK(sender IN (" in (definicion[0] or "")
    faltan = {"origin", "handler", "thread", "task_key"} - columnas(db, "messages")
    if not cerrada and not faltan:
        return

    # OJO con el candado: `with db:` no abre transaccion con DDL ni con SELECT, solo
    # con DML, asi que un re-chequeo dentro de `with db:` NO protege de nada y dos
    # procesos pueden entrar los dos (comprobado: "table messages_nuevo already
    # exists"). Hace falta BEGIN IMMEDIATE, que toma el candado de escritura antes de
    # mirar; el que pierde espera por busy_timeout y encuentra la base ya migrada.
    # `PRAGMA foreign_keys` no tiene efecto dentro de una transaccion: va antes.
    if cerrada:
        db.execute("PRAGMA foreign_keys = OFF")
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                actual = db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'"
                ).fetchone()
                if actual is None or "CHECK(sender IN (" not in (actual[0] or ""):
                    db.execute("ROLLBACK")  # otro proceso migro mientras esperabamos
                    return
                db.execute("DROP TABLE IF EXISTS messages_nuevo")
                db.execute(f"""
                    CREATE TABLE messages_nuevo (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        sender TEXT NOT NULL,
                        recipient TEXT NOT NULL,
                        body TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        ack_at TEXT,
                        reply_to INTEGER REFERENCES messages(id),
                        origin TEXT NOT NULL DEFAULT 'interactiva' CHECK(origin IN {ORIGENES}),
                        handler TEXT NOT NULL DEFAULT 'cualquiera' CHECK(handler IN {DESTINATARIOS}),
                        thread INTEGER,
                        task_key TEXT,
                        CHECK(sender != recipient)
                    )
                """)
                db.execute(
                    "INSERT INTO messages_nuevo(id, sender, recipient, body, created_at, ack_at, reply_to) "
                    "SELECT id, sender, recipient, body, created_at, ack_at, reply_to FROM messages"
                )
                db.execute("DROP TABLE messages")
                db.execute("ALTER TABLE messages_nuevo RENAME TO messages")
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        finally:
            db.execute("PRAGMA foreign_keys = ON")
    else:
        db.execute("BEGIN IMMEDIATE")
        try:
            # Se vuelven a leer las columnas con el candado tomado: otro proceso pudo
            # agregarlas, y un ALTER repetido falla con "duplicate column name".
            faltan = {"origin", "handler", "thread", "task_key"} - columnas(db, "messages")
            for columna, definicion_columna in (
                ("origin", f"TEXT NOT NULL DEFAULT 'interactiva' CHECK(origin IN {ORIGENES})"),
                ("handler", f"TEXT NOT NULL DEFAULT 'cualquiera' CHECK(handler IN {DESTINATARIOS})"),
                ("thread", "INTEGER"),
                ("task_key", "TEXT"),
            ):
                if columna in faltan:
                    db.execute(f"ALTER TABLE messages ADD COLUMN {columna} {definicion_columna}")
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise

    with db:
        # `handler` sale de un hecho: tener fila en `dispatches` es haberse enviado
        # con --auto, o sea dirigido al receptor automatico.
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dispatches'").fetchone():
            db.execute("UPDATE messages SET handler='automatica' WHERE id IN (SELECT message_id FROM dispatches)")
            # `origin` del historial es INFERIDO, no un dato: una respuesta la escribio
            # el receptor cuando ella y el mensaje que contesta pasaron por la cola
            # automatica. Queda dicho en el README para no leerlo como certeza.
            db.execute("""
                UPDATE messages SET origin='automatica' WHERE reply_to IS NOT NULL
                  AND id IN (SELECT message_id FROM dispatches)
                  AND reply_to IN (SELECT message_id FROM dispatches)
            """)
        db.execute("""
            WITH RECURSIVE raiz(id, root) AS (
                SELECT id, id FROM messages WHERE reply_to IS NULL
                UNION ALL
                SELECT m.id, r.root FROM messages m JOIN raiz r ON m.reply_to = r.id
            )
            UPDATE messages SET thread = (SELECT root FROM raiz WHERE raiz.id = messages.id)
            WHERE thread IS NULL
        """)


def connect():
    path = database_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        raise RuntimeError(f"mailbox directory must be private (0700): {path.parent}")
    for attempt in range(5):
        db = sqlite3.connect(path, timeout=10)
        try:
            initialize(db)
            return db
        except sqlite3.OperationalError as error:
            db.close()
            if "locked" not in str(error).lower() or attempt == 4:
                raise
            time.sleep(0.1 * (attempt + 1))


def agentes(db, solo_activos=True):
    clausula = " WHERE enabled = 1" if solo_activos else ""
    return [dict(fila) for fila in db.execute(f"SELECT * FROM agents{clausula} ORDER BY name")]


def nombres_de_agentes(db, solo_activos=True):
    return [agente["name"] for agente in agentes(db, solo_activos)]


def exigir_agente(db, nombre, papel):
    """Valida contra el registro, no contra una lista en el codigo."""
    fila = db.execute("SELECT * FROM agents WHERE name = ?", (nombre,)).fetchone()
    if fila is None:
        conocidos = ", ".join(nombres_de_agentes(db, solo_activos=False)) or "ninguno"
        raise ValueError(f"{papel} desconocido: {nombre!r}. Registrados: {conocidos}. "
                         f"Se agrega con: buzon.py agent add {nombre} --kind interactiva")
    if not fila["enabled"]:
        raise ValueError(f"el agente {nombre!r} esta desactivado; reactivalo con: buzon.py agent enable {nombre}")
    return dict(fila)


def exigir_receptor(db, destinatario, auto):
    """--auto solo vale hacia un agente que tenga receptor automatico declarado."""
    if auto and not destinatario.get("receiver"):
        raise ValueError(
            f"{destinatario['name']!r} no tiene receptor automatico, asi que --auto fallaria: "
            f"declaralo con 'buzon.py agent receptor {destinatario['name']} --set <adaptador>' "
            f"o manda el mensaje sin --auto")
    return destinatario


HORAS_SIN_MIRAR = 3


def sesion_dormida(destinatario, auto):
    """Advierte si el destinatario no mira su bandeja desde hace rato.

    No bloquea el envio: dejar el mensaje escrito sigue siendo lo correcto, y el buzon
    es durable a proposito. Pero quien escribe tiene que saber que del otro lado puede
    no haber nadie, porque con --auto el receptor de solo lectura responde y con un
    pedido que exige editar o revisar un PR NO hay quien lo atienda: el trabajo se queda
    parado en silencio y el que pregunto se enters horas despues. Me paso hoy tres
    veces con el mismo pedido de revision.
    """
    if auto or destinatario["kind"] != "interactiva":
        return ""
    visto = destinatario.get("last_inbox_at")
    if visto is None:
        return (f"buzon: aviso — {destinatario['name']} no ha mirado su bandeja "
                "desde que se registra esa marca; el mensaje queda escrito, pero si "
                "necesitas respuesta no cuentes con que llegue sola.")
    try:
        horas = (datetime.now(timezone.utc) - datetime.fromisoformat(visto)).total_seconds() / 3600
    except (TypeError, ValueError):
        return ""
    if horas < HORAS_SIN_MIRAR:
        return ""
    return (f"buzon: aviso — {destinatario['name']} no mira su bandeja desde hace "
            f"{int(horas)} h. El mensaje queda escrito, pero su receptor automatico solo "
            "lee: un pedido que exija editar o revisar un PR se va a quedar parado.")


def cola_sin_cuota(db, destinatario, auto):
    """Advierte al encolar un --auto cuando la cuota del dia ya se agoto.

    Es el espejo del aviso de sesion dormida, y salio de un caso real: durante la tarde
    se encolaron seis pedidos de revision con --auto, con la cuota agotada desde la
    manana, asi que ninguno podia correr hasta la medianoche siguiente y el que los
    mandaba no recibia ninguna senal. Un mensaje en esa cola no sale en ningun inbox, asi
    que el silencio es total. Si ademas la sesion del destinatario esta despierta, se
    dice: mandarlo SIN --auto lo atiende hoy.
    """
    if not auto:
        return ""
    try:
        despachador = runpy.run_path(str(Path(__file__).resolve().with_name("agent-dispatch")))
        tope = despachador["_tope_diario"]()
    except Exception:
        return ""
    medianoche = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    gastados = db.execute(
        "SELECT COALESCE(SUM(attempts), 0) FROM dispatches WHERE started_at >= ?",
        (medianoche,)).fetchone()[0]
    if gastados < tope:
        return ""
    texto = (f"buzon: aviso — la cuota del receptor automatico esta agotada "
             f"({gastados} de {tope}), asi que esto no se procesa hasta la proxima "
             "medianoche UTC y mientras tanto no aparece en ningun inbox.")
    visto = destinatario.get("last_inbox_at")
    if visto:
        try:
            horas = (datetime.now(timezone.utc) - datetime.fromisoformat(visto)).total_seconds() / 3600
            if horas < HORAS_SIN_MIRAR:
                texto += (f" La sesion de {destinatario['name']} miro su bandeja hace "
                          f"{int(horas * 60)} min: mandalo SIN --auto y lo atiende hoy.")
        except (TypeError, ValueError):
            pass
    return texto


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def body_from(args):
    if args.text is not None:
        body = args.text
    elif args.file == "-":
        body = sys.stdin.read(MAX_BODY + 1)
    else:
        body = Path(args.file).read_text(encoding="utf-8")
    if not body.strip():
        raise ValueError("message is empty")
    if len(body) > MAX_BODY:
        raise ValueError(f"message exceeds {MAX_BODY} characters")
    return body


def insert(db, sender, recipient, body, reply_to=None, auto=False, origin="interactiva",
           handler=None, task_key=None, turnos=1, write=False, repo=None):
    """Guarda el mensaje con su identificacion completa.

    `origin` dice quien lo escribio de verdad y `handler` a quien le toca tratarlo:
    juntos son lo que evita que una instancia automatica conteste en nombre de una
    sesion interactiva, o que las dos hagan la misma tarea. `write`/`repo` son el modo
    de escritura con revision cruzada (FR-017): sin sesion interactiva de nadie, pero
    nada se pushea sin que el otro agente lo apruebe (`agent-dispatch` lo hace cumplir).
    """
    if origin not in ORIGENES:
        raise ValueError(f"origen invalido: {origin!r}; use uno de {', '.join(ORIGENES)}")
    if handler is None:
        handler = "automatica" if auto else "cualquiera"
    if handler not in DESTINATARIOS:
        raise ValueError(f"destinatario invalido: {handler!r}; use uno de {', '.join(DESTINATARIOS)}")
    if auto and handler != "automatica":
        raise ValueError("--auto implica --para automatica")
    if write and not task_key:
        raise ValueError("--write exige --tarea para identificar la tarea")
    if write and not auto:
        raise ValueError("--write exige --auto: la escritura con revision cruzada no espera a nadie")
    if write and not repo:
        raise ValueError("--write exige --repo: la tarea tiene que decir donde escribe")
    if repo and not write:
        raise ValueError("--repo solo tiene sentido con --write")
    if task_key is not None and not NOMBRE_DE_AGENTE.match(task_key):
        raise ValueError("la clave de tarea usa minusculas, numeros y guiones, de 2 a 32 caracteres")
    modo = "escritura" if write else "lectura"
    with db:
        if task_key is not None:
            abierta = db.execute(
                "SELECT id, recipient FROM messages WHERE task_key = ? AND ack_at IS NULL", (task_key,)
            ).fetchone()
            if abierta:
                raise ValueError(f"la tarea {task_key!r} ya esta abierta en #{abierta['id']} "
                                 f"(para {abierta['recipient']}); acusala antes de volver a pedirla")
        cur = db.execute(
            "INSERT INTO messages(sender, recipient, body, created_at, reply_to, origin, handler, "
            "task_key, modo, repo) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sender, recipient, body, now(), reply_to, origin, handler, task_key, modo, repo),
        )
        # El hilo lo fija el disparador `hilo_por_defecto`, no esta funcion: la raiz del
        # intercambio tiene que quedar puesta aunque escriba otro programa.
        if auto:
            # UN turno por omision, no dos. Con dos, la respuesta automatica dispara
            # otro turno automatico, y eso es como empezo la cadena de los mensajes
            # #59 al #81: veinte mensajes entre dos receptores que se contestaban
            # solos y se comieron la cuota del dia. Quien quiera ida y vuelta la pide
            # con --turnos 2 y sabe lo que esta pidiendo.
            db.execute(
                "INSERT INTO dispatches(message_id, turns_left, state) VALUES (?, ?, 'pending')",
                (cur.lastrowid, turnos),
            )
    etiquetas = [] if origin == "interactiva" else [origin]
    if handler != "cualquiera":
        etiquetas.append(f"para {handler}")
    if task_key:
        etiquetas.append(f"tarea {task_key}")
    sufijo = f" [{', '.join(etiquetas)}]" if etiquetas else ""
    print(f"sent #{cur.lastrowid} {sender} -> {recipient}{sufijo}")
    return cur.lastrowid


def registrar_respuesta(db, sender, recipient, body, reply_to=None, origin="automatica",
                        turns_left=None):
    """Inserta una respuesta ya identificada. La usa el receptor automatico.

    Existe para que el marcado no dependa de que cada escritor se acuerde: el
    despachador llama a esto en vez de hacer su propio INSERT, y asi el origen y el
    hilo quedan puestos por el mismo codigo que los prueba. Devuelve el id nuevo.
    Debe llamarse dentro de una transaccion del llamador.
    """
    if origin not in ORIGENES:
        raise ValueError(f"origen invalido: {origin!r}")
    cur = db.execute(
        "INSERT INTO messages(sender, recipient, body, created_at, reply_to, origin, handler) "
        "VALUES (?, ?, ?, ?, ?, ?, 'cualquiera')",
        (sender, recipient, body, now(), reply_to, origin),
    )
    # El hilo lo pone el disparador de la base, igual que para cualquier otro escritor.
    if turns_left:
        db.execute("INSERT INTO dispatches(message_id, turns_left, state) VALUES (?, ?, 'pending')",
                   (cur.lastrowid, turns_left))
    return cur.lastrowid


def cola_automatica(db):
    """Cuantos mensajes espera el receptor automatico, y desde cuando el mas viejo.

    Devuelve (cantidad, minutos del mas viejo). Sirve para que un bloqueo silencioso
    —cuota agotada, receptor detenido— se pueda ver sin mirar la base a mano.
    """
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dispatches'").fetchone():
        return 0, 0
    fila = db.execute("""
        SELECT COUNT(*) AS total, MIN(m.created_at) AS mas_viejo
        FROM dispatches d JOIN messages m ON m.id = d.message_id
        WHERE d.state IN ('pending', 'running') AND m.ack_at IS NULL
    """).fetchone()
    if not fila or not fila["total"]:
        return 0, 0
    try:
        nacido = datetime.fromisoformat(fila["mas_viejo"])
        espera = int((datetime.now(timezone.utc) - nacido).total_seconds() // 60)
    except (TypeError, ValueError):
        espera = 0
    return fila["total"], max(espera, 0)


def rows(db, agent, include_all=False, handler=None, thread=None):
    clause = "" if include_all else """AND m.ack_at IS NULL AND NOT EXISTS (
        SELECT 1 FROM dispatches d WHERE d.message_id = m.id AND d.state IN ('pending', 'running')
    )"""
    parametros = [agent]
    if handler:
        clause += " AND m.handler = ?"
        parametros.append(handler)
    if thread:
        clause += " AND m.thread = ?"
        parametros.append(thread)
    return db.execute(
        f"SELECT m.* FROM messages m WHERE m.recipient = ? {clause} ORDER BY m.id LIMIT 100",
        parametros,
    ).fetchall()


def print_rows(messages, as_json=False):
    if as_json:
        print(json.dumps([dict(row) for row in messages], ensure_ascii=False))
        return
    if not messages:
        print("no messages")
        return
    for row in messages:
        state = "pending" if row["ack_at"] is None else "acknowledged"
        if "withdrawn_at" in row.keys() and row["withdrawn_at"]:
            state = "RETIRADO-POR-EL-EMISOR"
        reply = f" reply-to=#{row['reply_to']}" if row["reply_to"] else ""
        claves = row.keys()
        # El origen se muestra SIEMPRE que sea automatico: es la marca que evita leer
        # como criterio de un agente lo que escribio su receptor sin persona detras.
        marca = " ESCRITO-POR-RECEPTOR-AUTOMATICO" if "origin" in claves and row["origin"] == "automatica" else ""
        para = f" para={row['handler']}" if "handler" in claves and row["handler"] != "cualquiera" else ""
        hilo = f" hilo=#{row['thread']}" if "thread" in claves and row["thread"] and row["thread"] != row["id"] else ""
        tarea = f" tarea={row['task_key']}" if "task_key" in claves and row["task_key"] else ""
        print(f"#{row['id']} {row['created_at']} {row['sender']} -> {row['recipient']} "
              f"[{state}]{reply}{para}{hilo}{tarea}{marca}")
        print(row["body"])
        print()


OWNER_PATTERN = re.compile(r"^([a-z][a-z0-9-]{1,31}|auto:[0-9]+)$")
DEFAULT_LEASE_MINUTES = 30
MAX_LEASE_MINUTES = 240
CONFLICT_EXIT = 3


def check_owner(owner):
    """A registered agent name, or auto:<message id> for an automatic run."""
    if not OWNER_PATTERN.match(owner):
        raise ValueError(f"owner must be an agent name or auto:<id>, not {owner!r}")
    return owner


def normalize_repo(repo):
    path = Path(repo).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return str(Path(os.path.normpath(path)))


def normalize_path(raw):
    """Repo-relative, normalized. '.' means the whole repository."""
    text = str(raw).strip().replace("\\", "/").strip("/")
    cleaned = os.path.normpath(text) if text else "."
    if cleaned == "." or cleaned == "":
        return "."
    if cleaned.startswith("..") or Path(cleaned).is_absolute():
        raise ValueError(f"path must stay inside the repository: {raw!r}")
    return cleaned


def paths_overlap(one, other):
    """True when the same file could be touched twice.

    Prefix comparison on path boundaries, no globs: `src` collides with
    `src/a.js`, `src/a.js` and `src/b.js` do not, and `.` collides with all.
    """
    if one == "." or other == ".":
        return True
    return one == other or one.startswith(other + "/") or other.startswith(one + "/")


def live_leases(db, repo=None, moment=None):
    moment = time.time() if moment is None else moment
    if repo is None:
        return db.execute(
            "SELECT * FROM leases WHERE released_at IS NULL AND expires_at > ? ORDER BY id",
            (moment,),
        ).fetchall()
    return db.execute(
        "SELECT * FROM leases WHERE repo = ? AND released_at IS NULL AND expires_at > ? ORDER BY id",
        (repo, moment),
    ).fetchall()


def lease_conflicts(db, repo, owner, wanted):
    """Live leases of ANOTHER owner that overlap any of the wanted paths."""
    return [
        row
        for row in live_leases(db, repo)
        if row["owner"] != owner and any(paths_overlap(path, row["path"]) for path in wanted)
    ]


def print_conflicts(conflicts, as_json=False):
    if as_json:
        print(json.dumps({"conflicts": [dict(row) for row in conflicts]}, ensure_ascii=False))
        return
    for row in conflicts:
        vence = datetime.fromtimestamp(row["expires_at"], timezone.utc).isoformat(timespec="seconds")
        nota = f" ({row['note']})" if row["note"] else ""
        print(f"#{row['id']} {row['repo']}/{row['path']} held by {row['owner']} until {vence}{nota}", file=sys.stderr)


def print_leases(leases, as_json=False):
    if as_json:
        print(json.dumps([dict(row) for row in leases], ensure_ascii=False))
        return
    if not leases:
        print("no leases")
        return
    for row in leases:
        vence = datetime.fromtimestamp(row["expires_at"], timezone.utc).isoformat(timespec="seconds")
        estado = "released" if row["released_at"] else ("live" if row["expires_at"] > time.time() else "expired")
        nota = f" ({row['note']})" if row["note"] else ""
        print(f"#{row['id']} [{estado}] {row['owner']} {row['repo']}/{row['path']} until {vence}{nota}")


def take_leases(db, owner, repo, wanted, minutes, note, message_id):
    """All or nothing: one conflict and nothing is taken."""
    moment = time.time()
    expires = moment + minutes * 60
    with db:
        conflicts = lease_conflicts(db, repo, owner, wanted)
        if conflicts:
            return None, conflicts
        taken = []
        for path in wanted:
            mine = db.execute(
                "SELECT * FROM leases WHERE repo = ? AND path = ? AND owner = ? AND released_at IS NULL",
                (repo, path, owner),
            ).fetchone()
            if mine:
                # Volver a pedir lo propio renueva, no duplica: asi un worker puede
                # extender su arriendo sin tener que soltarlo primero.
                db.execute(
                    "UPDATE leases SET expires_at = ?, note = COALESCE(?, note), message_id = COALESCE(?, message_id) WHERE id = ?",
                    (expires, note, message_id, mine["id"]),
                )
                taken.append(mine["id"])
                continue
            cur = db.execute(
                "INSERT INTO leases(repo, path, owner, message_id, note, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (repo, path, owner, message_id, note, now(), expires),
            )
            taken.append(cur.lastrowid)
    return taken, []


def imprimir_agentes(lista, como_json=False):
    if como_json:
        print(json.dumps(lista, ensure_ascii=False))
        return
    if not lista:
        print("registro vacio")
        return
    for agente in lista:
        estado = "activo" if agente["enabled"] else "desactivado"
        nota = f" ({agente['note']})" if agente["note"] else ""
        receptor = f" receptor={agente['receiver']}" if agente.get("receiver") else " sin receptor"
        print(f"{agente['name']:<16} {agente['kind']:<12} {estado:<12}{receptor:<22} desde {agente['added_at']}{nota}")


def retirar(db, emisor, ident, motivo):
    """El emisor se desdice de su propio mensaje, si el otro todavia no lo trato.

    El buzon tenia `devolver` (sacar algo de la cola automatica) y `ack` (el que recibe
    da por tratado), pero nada para el que escribio. Hoy mande un aviso que despues
    corregi y no pude retirar: quedo pendiente en la bandeja del otro, afirmando algo
    que ya no era cierto. Si ya esta acusado NO se retira, porque el otro ya lo leyo y
    lo que corresponde entonces es mandar la correccion, no borrar la historia.
    """
    fila = db.execute("SELECT * FROM messages WHERE id = ?", (ident,)).fetchone()
    if fila is None:
        raise ValueError(f"no existe el mensaje #{ident}")
    if fila["sender"] != emisor:
        raise ValueError(f"el mensaje #{ident} no lo escribio {emisor}, lo escribio "
                         f"{fila['sender']}: solo el emisor puede retirar lo suyo")
    if fila["ack_at"] is not None:
        raise ValueError(f"el mensaje #{ident} ya esta acusado por {fila['recipient']}: "
                         "ya lo leyo, asi que manda la correccion en vez de retirarlo")
    with db:
        db.execute("UPDATE messages SET ack_at = ?, withdrawn_at = ?, withdrawn_reason = ? "
                   "WHERE id = ?", (now(), now(), motivo[:200], ident))
        # Si estaba esperando al receptor automatico, se cierra su despacho: un mensaje
        # retirado no debe gastar una ejecucion del dia.
        db.execute("UPDATE dispatches SET state = 'done', finished_at = ?, last_error = ? "
                   "WHERE message_id = ? AND state IN ('pending', 'running')",
                   (now(), f"retirado por el emisor: {motivo}"[:200], ident))
    print(f"#{ident} retirado de la bandeja de {fila['recipient']}: {motivo}")
    return 0


def devolver_a_interactiva(db, agente, ident, motivo):
    """Cancela el tratamiento automatico de un mensaje y lo deja visible al humano.

    Existe por un caso real: cuatro pedidos de revision de un Pull Request salieron
    con --auto y quedaron en una cola que el receptor de solo lectura no puede
    atender, invisibles en toda bandeja durante horas. Contestarlos automaticamente
    habria producido respuestas malas en nombre de un agente; borrarlos habria perdido
    el pedido. Devolverlos es lo correcto: el mensaje sobrevive y lo trata quien puede.
    """
    fila = db.execute("SELECT * FROM messages WHERE id = ? AND recipient = ?", (ident, agente)).fetchone()
    if fila is None:
        raise ValueError(f"el mensaje #{ident} no esta dirigido a {agente}")
    if fila["ack_at"] is not None:
        raise ValueError(f"el mensaje #{ident} ya esta acusado")
    despacho = db.execute(
        "SELECT state FROM dispatches WHERE message_id = ? AND state IN ('pending', 'running')", (ident,)
    ).fetchone()
    if despacho is None:
        raise ValueError(f"el mensaje #{ident} no esta esperando al receptor automatico")
    with db:
        db.execute(
            "UPDATE dispatches SET state = 'done', finished_at = ?, last_error = ? WHERE message_id = ?",
            (now(), f"devuelto a la bandeja interactiva: {motivo}"[:200], ident),
        )
        # El destinatario pasa a `interactiva` para que el motivo quede en el dato y no
        # solo en una nota: el mensaje ya no es del receptor automatico.
        db.execute("UPDATE messages SET handler = 'interactiva' WHERE id = ?", (ident,))
    print(f"#{ident} devuelto a la bandeja de {agente}: {motivo}")
    return 0


def run_agent(db, args):
    if args.agent_command == "add":
        if not NOMBRE_DE_AGENTE.match(args.name):
            raise ValueError("el nombre usa minusculas, numeros y guiones, de 2 a 32 caracteres")
        if db.execute("SELECT 1 FROM agents WHERE name = ?", (args.name,)).fetchone():
            raise ValueError(f"el agente {args.name!r} ya esta registrado")
        with db:
            db.execute("INSERT INTO agents(name, kind, added_at, note, receiver) VALUES (?, ?, ?, ?, ?)",
                       (args.name, args.kind, now(), args.note, args.receiver))
        print(f"registrado {args.name} ({args.kind})")
        return 0

    if args.agent_command == "list":
        imprimir_agentes(agentes(db, solo_activos=not args.all), args.json)
        return 0

    if args.agent_command in ("enable", "disable"):
        activo = 1 if args.agent_command == "enable" else 0
        with db:
            cur = db.execute("UPDATE agents SET enabled = ? WHERE name = ?", (activo, args.name))
        if not cur.rowcount:
            raise ValueError(f"el agente {args.name!r} no esta registrado")
        print(f"{args.name}: {'activo' if activo else 'desactivado'}")
        return 0

    if args.agent_command == "receptor":
        exigir_agente(db, args.name, "agente")
        with db:
            db.execute("UPDATE agents SET receiver = ? WHERE name = ?",
                       (None if args.clear else args.valor, args.name))
        print(f"{args.name}: {'sin receptor' if args.clear else 'receptor ' + args.valor}")
        return 0

    if args.agent_command == "remove":
        usados = db.execute("SELECT COUNT(*) FROM messages WHERE sender = ? OR recipient = ?",
                            (args.name, args.name)).fetchone()[0]
        if usados:
            # Borrarlo dejaria mensajes apuntando a un agente inexistente y el
            # historial no se puede releer. Desactivar conserva el hilo completo.
            raise ValueError(f"{args.name!r} tiene {usados} mensaje(s) en el historial; "
                             f"usa: buzon.py agent disable {args.name}")
        with db:
            cur = db.execute("DELETE FROM agents WHERE name = ?", (args.name,))
        if not cur.rowcount:
            raise ValueError(f"el agente {args.name!r} no esta registrado")
        print(f"borrado {args.name}")
        return 0

    raise ValueError(f"subcomando de agent desconocido: {args.agent_command}")


def run_lease(db, args):
    if args.lease_command == "take":
        owner = check_owner(args.owner)
        repo = normalize_repo(args.repo)
        wanted = sorted({normalize_path(path) for path in args.path})
        taken, conflicts = take_leases(db, owner, repo, wanted, args.minutes, args.note, args.message_id)
        if conflicts:
            print_conflicts(conflicts, args.json)
            if not args.json:
                print("lease: nothing taken", file=sys.stderr)
            return CONFLICT_EXIT
        filas = db.execute(
            f"SELECT * FROM leases WHERE id IN ({','.join('?' * len(taken))}) ORDER BY id", taken
        ).fetchall()
        print_leases(filas, args.json)
        return 0

    if args.lease_command == "list":
        if args.all:
            repo = normalize_repo(args.repo) if args.repo else None
            consulta = "SELECT * FROM leases" + (" WHERE repo = ?" if repo else "") + " ORDER BY id"
            print_leases(db.execute(consulta, (repo,) if repo else ()).fetchall(), args.json)
        else:
            print_leases(live_leases(db, normalize_repo(args.repo) if args.repo else None), args.json)
        return 0

    if args.lease_command == "release":
        owner = check_owner(args.owner)
        with db:
            if args.mine:
                repo = normalize_repo(args.repo) if args.repo else None
                consulta = "UPDATE leases SET released_at = ? WHERE owner = ? AND released_at IS NULL"
                parametros = [now(), owner]
                if repo:
                    consulta += " AND repo = ?"
                    parametros.append(repo)
                cur = db.execute(consulta, parametros)
            else:
                marcas = ",".join("?" * len(args.id))
                cur = db.execute(
                    f"UPDATE leases SET released_at = ? WHERE owner = ? AND released_at IS NULL AND id IN ({marcas})",
                    [now(), owner, *args.id],
                )
        # Soltar lo que no es tuyo no es un error silencioso: se dice.
        if not cur.rowcount:
            raise ValueError(f"no live leases of {owner} matched")
        print(f"released {cur.rowcount} lease(s)")
        return 0

    if args.lease_command == "steal":
        owner = check_owner(args.owner)
        victima = db.execute("SELECT * FROM leases WHERE id = ?", (args.id,)).fetchone()
        if victima is None:
            raise ValueError(f"lease #{args.id} does not exist")
        if victima["owner"] == owner:
            raise ValueError("that lease is already yours; use take to extend it")
        if victima["released_at"]:
            raise ValueError(f"lease #{args.id} was already released")
        with db:
            # La fila robada se conserva con el motivo: el relevo queda auditable y no
            # se pierde quien lo tenia antes.
            db.execute(
                "UPDATE leases SET released_at = ?, note = ? WHERE id = ?",
                (now(), f"stolen by {owner}: {args.reason}"[:500], args.id),
            )
        taken, conflicts = take_leases(
            db, owner, victima["repo"], [victima["path"]], args.minutes,
            f"taken over from {victima['owner']}: {args.reason}"[:500], None,
        )
        if conflicts:
            print_conflicts(conflicts, args.json)
            return CONFLICT_EXIT
        filas = db.execute("SELECT * FROM leases WHERE id = ?", (taken[0],)).fetchall()
        print_leases(filas, args.json)
        return 0

    raise ValueError(f"unknown lease command {args.lease_command}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def content_options(p):
        group = p.add_mutually_exclusive_group(required=True)
        group.add_argument("--text", help="short message text")
        group.add_argument("--file", help="UTF-8 file, or - for stdin")

    def identificacion(p):
        # --origin existe para que un receptor automatico NO pueda pasar por humano:
        # lo marca el que envia, y el que lee lo ve siempre.
        p.add_argument("--origin", choices=ORIGENES, default="interactiva",
                       help="quien escribe de verdad este mensaje")
        p.add_argument("--para", dest="handler", choices=DESTINATARIOS,
                       help="a quien le toca tratarlo (por omision: cualquiera, o automatica con --auto)")
        p.add_argument("--tarea", dest="task_key",
                       help="clave de tarea: impide que se pida dos veces mientras siga abierta")
        p.add_argument("--turnos", type=int, choices=(1, 2), default=1,
                       help="turnos de la cadena automatica; 2 deja que la respuesta dispare otro turno")

    send = sub.add_parser("send", help="send a message")
    send.add_argument("sender")
    send.add_argument("recipient")
    content_options(send)
    send.add_argument("--auto", action="store_true", help="wake a bounded background responder")
    send.add_argument("--write", action="store_true",
                      help="escritura con revision cruzada (FR-017): exige --auto y --repo")
    send.add_argument("--repo", help="ruta del repositorio donde se escribe (con --write)")
    identificacion(send)

    inbox = sub.add_parser("inbox", help="show pending messages")
    inbox.add_argument("agent")
    inbox.add_argument("--all", action="store_true", help="include acknowledged messages")
    inbox.add_argument("--para", dest="handler", choices=DESTINATARIOS, help="filtrar por destinatario")
    inbox.add_argument("--hilo", dest="thread", type=int, help="solo los mensajes de ese hilo")
    inbox.add_argument("--json", action="store_true")

    wait = sub.add_parser("wait", help="wait for pending messages")
    wait.add_argument("agent")
    wait.add_argument("--timeout", type=int, default=60, help="seconds; 0 waits indefinitely")
    wait.add_argument("--json", action="store_true")

    ack = sub.add_parser("ack", help="acknowledge a message after handling it")
    ack.add_argument("agent")
    ack.add_argument("id", type=int)

    reply = sub.add_parser("reply", help="reply to a message addressed to you")
    reply.add_argument("sender")
    reply.add_argument("id", type=int)
    content_options(reply)
    reply.add_argument("--auto", action="store_true", help="wake a bounded background responder")
    reply.add_argument("--otra-vez", dest="otra_vez", action="store_true",
                       help="responder un mensaje que ya respondiste")
    identificacion(reply)

    agente = sub.add_parser("agent", help="registro de agentes del canal")
    agente_sub = agente.add_subparsers(dest="agent_command", required=True)
    alta = agente_sub.add_parser("add", help="registrar un agente nuevo")
    alta.add_argument("name")
    alta.add_argument("--kind", choices=CLASES_DE_AGENTE, required=True)
    alta.add_argument("--note")
    alta.add_argument("--receptor", dest="receiver",
                      help="adaptador con el que el receptor automatico puede atenderlo")
    listado = agente_sub.add_parser("list", help="ver el registro")
    listado.add_argument("--all", action="store_true", help="incluir los desactivados")
    listado.add_argument("--json", action="store_true")
    for accion, ayuda in (("enable", "reactivar"), ("disable", "desactivar sin perder historial")):
        sub_accion = agente_sub.add_parser(accion, help=ayuda)
        sub_accion.add_argument("name")
    baja = agente_sub.add_parser("remove", help="borrar del registro (solo si no tiene mensajes)")
    baja.add_argument("name")
    receptor = agente_sub.add_parser("receptor", help="declarar o quitar su receptor automatico")
    receptor.add_argument("name")
    grupo_receptor = receptor.add_mutually_exclusive_group(required=True)
    grupo_receptor.add_argument("--set", dest="valor")
    grupo_receptor.add_argument("--clear", action="store_true")

    status = sub.add_parser("status", help="show pending counts")
    status.add_argument("--json", action="store_true")

    devolver = sub.add_parser(
        "devolver",
        help="saca un mensaje de la cola automatica y lo devuelve a la bandeja interactiva")
    devolver.add_argument("agent", help="destinatario del mensaje")
    devolver.add_argument("id", type=int)
    devolver.add_argument("--motivo", default="devuelto a la bandeja interactiva")

    retirar_cmd = sub.add_parser(
        "retirar",
        help="el emisor se desdice de su propio mensaje, si el otro no lo trato todavia")
    retirar_cmd.add_argument("sender", help="quien lo escribio: solo el emisor retira lo suyo")
    retirar_cmd.add_argument("id", type=int)
    retirar_cmd.add_argument("--motivo", required=True)

    lease = sub.add_parser("lease", help="declare who edits which paths")
    lease_sub = lease.add_subparsers(dest="lease_command", required=True)

    take = lease_sub.add_parser("take", help="claim paths; all or nothing")
    take.add_argument("owner", help="a registered agent name, or auto:<message id>")
    take.add_argument("--repo", required=True)
    take.add_argument("--path", action="append", required=True, help="repeatable; . means the whole repo")
    take.add_argument("--minutes", type=int, default=DEFAULT_LEASE_MINUTES)
    take.add_argument("--note")
    take.add_argument("--message-id", type=int)
    take.add_argument("--json", action="store_true")

    show = lease_sub.add_parser("list", help="show leases")
    show.add_argument("--repo")
    show.add_argument("--all", action="store_true", help="include released and expired")
    show.add_argument("--json", action="store_true")

    free = lease_sub.add_parser("release", help="release your own leases")
    free.add_argument("owner")
    free.add_argument("id", type=int, nargs="*", help="lease ids; omit with --mine to release all yours")
    free.add_argument("--mine", action="store_true", help="release every live lease of this owner")
    free.add_argument("--repo", help="with --mine, limit to this repository")

    steal = lease_sub.add_parser("steal", help="take over someone else's lease, on the record")
    steal.add_argument("owner")
    steal.add_argument("id", type=int)
    steal.add_argument("--reason", required=True)
    steal.add_argument("--minutes", type=int, default=DEFAULT_LEASE_MINUTES)
    steal.add_argument("--json", action="store_true")

    args = parser.parse_args()
    if args.command == "send" and args.sender == args.recipient:
        parser.error("sender and recipient must differ")
    if args.command == "wait" and args.timeout < 0:
        parser.error("timeout must be >= 0")
    if args.command == "lease" and getattr(args, "minutes", 1) is not None:
        if args.lease_command in ("take", "steal") and not 1 <= args.minutes <= MAX_LEASE_MINUTES:
            parser.error(f"minutes must be between 1 and {MAX_LEASE_MINUTES}")
    if args.command == "lease" and args.lease_command == "release" and not args.id and not args.mine:
        parser.error("give lease ids or --mine")

    os.umask(0o077)
    try:
        with connect() as db:
            if args.command == "agent":
                resultado = run_agent(db, args)
                if resultado:
                    return resultado
            elif args.command == "send":
                exigir_agente(db, args.sender, "remitente")
                destino = exigir_agente(db, args.recipient, "destinatario")
                exigir_receptor(db, destino, args.auto)
                aviso = sesion_dormida(destino, args.auto)
                if aviso:
                    print(aviso, file=sys.stderr)
                aviso_cuota = cola_sin_cuota(db, destino, args.auto)
                if aviso_cuota:
                    print(aviso_cuota, file=sys.stderr)
                insert(db, args.sender, args.recipient, body_from(args), auto=args.auto,
                       origin=args.origin, handler=args.handler, task_key=args.task_key,
                       turnos=args.turnos, write=args.write, repo=args.repo)
            elif args.command == "inbox":
                exigir_agente(db, args.agent, "agente")
                filas = rows(db, args.agent, args.all, args.handler, args.thread)
                with db:
                    db.execute("UPDATE agents SET last_inbox_at = ? WHERE name = ?",
                               (now(), args.agent))
                print_rows(filas, args.json)
            elif args.command == "wait":
                exigir_agente(db, args.agent, "agente")
                deadline = time.monotonic() + args.timeout if args.timeout else None
                while True:
                    pending = rows(db, args.agent)
                    if pending:
                        print_rows(pending, args.json)
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        print_rows([], args.json)
                        return 2
                    time.sleep(0.5)
            elif args.command == "ack":
                with db:
                    cur = db.execute(
                        "UPDATE messages SET ack_at = COALESCE(ack_at, ?) WHERE id = ? AND recipient = ?",
                        (now(), args.id, args.agent),
                    )
                if not cur.rowcount:
                    raise ValueError(f"message #{args.id} is not addressed to {args.agent}")
                db.execute(
                    "UPDATE dispatches SET state = 'done', finished_at = ? WHERE message_id = ? AND state != 'done'",
                    (now(), args.id),
                )
                print(f"acknowledged #{args.id}")
            elif args.command == "reply":
                exigir_agente(db, args.sender, "remitente")
                original = db.execute("SELECT * FROM messages WHERE id = ?", (args.id,)).fetchone()
                if original is None or original["recipient"] != args.sender:
                    raise ValueError(f"message #{args.id} is not addressed to {args.sender}")
                # Contestar dos veces el mismo mensaje es la forma mas facil de
                # duplicar trabajo y de confundir el hilo. Se permite a proposito con
                # --otra-vez, pero no por descuido.
                if not args.otra_vez:
                    ya = db.execute(
                        "SELECT id, created_at FROM messages WHERE reply_to = ? AND sender = ? ORDER BY id LIMIT 1",
                        (args.id, args.sender),
                    ).fetchone()
                    if ya:
                        raise ValueError(
                            f"ya respondiste el #{args.id} en el #{ya['id']} ({ya['created_at']}); "
                            f"si de verdad quieres responder otra vez, agrega --otra-vez")
                exigir_receptor(db, exigir_agente(db, original["sender"], "destinatario"), args.auto)
                insert(db, args.sender, original["sender"], body_from(args), args.id, args.auto,
                       origin=args.origin, handler=args.handler, task_key=args.task_key,
                       turnos=args.turnos)
            elif args.command == "lease":
                resultado = run_lease(db, args)
                if resultado:
                    return resultado
            elif args.command == "retirar":
                exigir_agente(db, args.sender, "emisor")
                resultado = retirar(db, args.sender, args.id, args.motivo)
                if resultado:
                    return resultado
            elif args.command == "devolver":
                exigir_agente(db, args.agent, "agente")
                resultado = devolver_a_interactiva(db, args.agent, args.id, args.motivo)
                if resultado:
                    return resultado
            elif args.command == "status":
                # Se cuenta lo que el inbox de cada agente MUESTRA, usando la misma
                # funcion, no una consulta paralela. Antes era un COUNT(*) de todo lo
                # sin acusar, y eso contaba DOS VECES lo que esta en la cola automatica:
                # una aqui y otra en `automaticos_en_cola`. Visto en uso real: status
                # decia "codex: 4 pending" mientras `inbox codex` mostraba 2, que es
                # justo la clase de numero que no se puede creer. Asi los dos numeros
                # son disjuntos y suman el total.
                counts = {agent: len(rows(db, agent)) for agent in nombres_de_agentes(db)}
                # La cola automatica NO aparece en ningun inbox a proposito, para que
                # una sesion no conteste lo que ya tomo el receptor. El precio es que
                # un trabajo frenado (cuota diaria agotada, receptor caido) queda
                # invisible para todos: por eso se cuenta aqui, con su espera.
                # La clave lleva guion bajo a proposito: NOMBRE_DE_AGENTE no lo
                # admite, asi que nunca puede chocar con un agente registrado.
                counts["automaticos_en_cola"] = cola_automatica(db)[0]
                print(json.dumps(counts) if args.json else " ".join(f"{a}: {n} pending" for a, n in counts.items()))
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        print(f"buzon: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
