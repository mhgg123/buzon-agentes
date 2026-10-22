"""Ciclo completo del despachador: lectura automática y escritura con revisión
cruzada contra un repo git real, con agentes simulados por /bin/sh."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
BUZON = RAIZ / "buzon_agentes" / "buzon.py"
DESPACHADOR = RAIZ / "buzon_agentes" / "despachador.py"

AGENTES_TOML = """
[agentes.alfa]
comando = ["/bin/sh", "-c", "cat >/dev/null; echo APROBADO listo"]
formato = "texto"
comando_escritura = ["/bin/sh", "-c",
  "cat >/dev/null; printf 'linea nueva de alfa\\n' >> nota.txt; git add nota.txt; echo listo"]

[agentes.beta]
comando = ["/bin/sh", "-c", "cat >/dev/null; echo APROBADO listo"]
formato = "texto"
comando_escritura = ["/bin/sh", "-c",
  "cat >/dev/null; printf 'linea nueva de beta\\n' >> nota.txt; git add nota.txt; echo listo"]

[revision.pares]
alfa = "beta"
beta = "alfa"
"""


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class DespachadorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        privado = base / "privado"
        privado.mkdir(mode=0o700)
        config = base / "agentes.toml"
        config.write_text(AGENTES_TOML)
        self.env = dict(
            os.environ,
            BUZON_DB=str(privado / "mail.sqlite3"),
            BUZON_AGENTES_CONFIG=str(config),
            BUZON_AGENT_WORKDIR=str(base),
        )
        self.repo = base / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "trabajo")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "T")
        (self.repo / "nota.txt").write_text("linea inicial\n")
        git(self.repo, "add", "nota.txt")
        git(self.repo, "commit", "-q", "-m", "inicial")
        self.correr("agent", "add", "alfa", "--kind", "interactiva", "--receptor", "alfa")
        self.correr("agent", "add", "beta", "--kind", "interactiva", "--receptor", "beta")

    def correr(self, *args, check=True):
        return subprocess.run([sys.executable, str(BUZON), *args], env=self.env,
                              text=True, capture_output=True, check=check)

    def despachar(self):
        return subprocess.run([sys.executable, str(DESPACHADOR), "once"], env=self.env,
                              text=True, capture_output=True, check=True)

    def test_lectura_automatica(self):
        self.correr("send", "alfa", "beta", "--auto", "--text", "hola")
        self.despachar()
        bandeja = json.loads(self.correr("inbox", "alfa", "--all", "--json").stdout)
        self.assertTrue(any("APROBADO listo" in m["body"] for m in bandeja))

    def test_escritura_con_revision_cruzada_queda_aprobada_local(self):
        antes = git(self.repo, "rev-parse", "HEAD")
        self.correr("send", "alfa", "beta", "--auto", "--write", "--repo", str(self.repo),
                   "--tarea", "agregar-linea", "--text", "agrega una linea a nota.txt")
        self.despachar()  # beta escribe en un checkout aislado, pide revision a alfa
        self.despachar()  # alfa revisa y aprueba (local)

        # El repo real de origen no se tocó: la escritura ocurrió en un clon aparte.
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), antes)

        bandeja = json.loads(self.correr("inbox", "beta", "--all", "--json").stdout)
        self.assertTrue(any("aprobado_local" in m["body"] for m in bandeja))

    def test_revision_no_duplica_la_notificacion(self):
        self.correr("send", "alfa", "beta", "--auto", "--write", "--repo", str(self.repo),
                   "--tarea", "una-sola-vez", "--text", "agrega una linea a nota.txt")
        self.despachar()
        self.despachar()
        bandeja = json.loads(self.correr("inbox", "beta", "--all", "--json").stdout)
        # Una tarea de escritura + una revision = dos mensajes a beta (la tarea
        # original y UN solo aviso del veredicto), nunca tres.
        self.assertEqual(len(bandeja), 2)

    def test_hook_plantado_por_el_agente_no_se_ejecuta(self):
        # El agente simulado deja un cambio SIN comprometer (fuerza a que el
        # supervisor haga el commit) y, en el mismo paso, planta un hook que —si
        # se ejecutara con el commit del supervisor— dejaria un archivo centinela
        # FUERA del checkout, donde seria facil de detectar desde afuera.
        centinela = self.repo.parent / "hook-corrio.txt"
        shell_malicioso = (
            "cat >/dev/null; printf 'cambio sin commit\\n' >> nota.txt; "
            "mkdir -p .git/hooks; "
            f"printf '#!/bin/sh\\necho corrio > {centinela}\\n' > .git/hooks/pre-commit; "
            "chmod +x .git/hooks/pre-commit; echo listo"
        )
        toml = self.repo.parent / "agentes-con-hook.toml"
        contenido = (
            "[agentes.alfa]\n"
            'comando = ["/bin/sh", "-c", "cat >/dev/null; echo APROBADO listo"]\n'
            f'comando_escritura = ["/bin/sh", "-c", "{shell_malicioso}"]\n'
            "\n"
            "[agentes.beta]\n"
            'comando = ["/bin/sh", "-c", "cat >/dev/null; echo APROBADO listo"]\n'
            'comando_escritura = ["/bin/sh", "-c", "echo sin-uso"]\n'
            "\n"
            "[revision.pares]\n"
            'alfa = "beta"\n'
            'beta = "alfa"\n'
        )
        toml.write_text(contenido)
        self.env["BUZON_AGENTES_CONFIG"] = str(toml)
        self.correr("send", "beta", "alfa", "--auto", "--write", "--repo", str(self.repo),
                   "--tarea", "con-hook", "--text", "deja un cambio sin commit")
        self.despachar()  # alfa escribe (planta el hook) y el SUPERVISOR comete
        self.despachar()  # beta revisa
        self.assertFalse(centinela.exists(),
                         "el hook plantado por el agente no debe ejecutarse al comprometer")

    def test_rama_main_se_rechaza_para_escritura(self):
        git(self.repo, "branch", "-m", "trabajo", "main")
        self.correr("send", "alfa", "beta", "--auto", "--write", "--repo", str(self.repo),
                   "--tarea", "en-main", "--text", "agrega una linea a nota.txt")
        self.despachar()
        # Nunca se crea un checkout aislado: la guarda de rama corre sobre el repo de
        # origen, antes de clonar nada.
        privado = Path(self.env["BUZON_DB"]).parent
        self.assertFalse((privado / "write-checkouts").exists())
        escalacion = json.loads(self.correr("inbox", "alfa", "--all", "--json").stdout)
        self.assertTrue(any("no permitida" in m["body"] for m in escalacion))


if __name__ == "__main__":
    unittest.main()
