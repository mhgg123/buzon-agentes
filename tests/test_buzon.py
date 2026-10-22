"""Pruebas de humo del buzón: registro, envío, inbox, reply, ack, arriendos."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "buzon_agentes" / "buzon.py"


class BuzonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        privado = Path(self.tmp.name) / "privado"
        privado.mkdir(mode=0o700)
        self.env = dict(os.environ, BUZON_DB=str(privado / "mail.sqlite3"))

    def correr(self, *args, check=True):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args], env=self.env,
            text=True, capture_output=True, check=check,
        )

    def registrar(self, *nombres):
        for nombre in nombres:
            self.correr("agent", "add", nombre, "--kind", "interactiva")

    def test_intercambio_y_acuse(self):
        self.registrar("alfa", "beta")
        self.correr("send", "alfa", "beta", "--text", "hola")
        bandeja = json.loads(self.correr("inbox", "beta", "--json").stdout)
        self.assertEqual(bandeja[0]["body"], "hola")
        self.assertIsNone(bandeja[0]["ack_at"])
        self.correr("reply", "beta", str(bandeja[0]["id"]), "--text", "recibido")
        respuesta = json.loads(self.correr("inbox", "alfa", "--json").stdout)
        self.assertEqual(respuesta[0]["body"], "recibido")
        self.correr("ack", "alfa", str(respuesta[0]["id"]))
        self.assertEqual(json.loads(self.correr("inbox", "alfa", "--json").stdout), [])

    def test_remitente_no_registrado_se_rechaza(self):
        self.registrar("beta")
        resultado = self.correr("send", "alfa", "beta", "--text", "hola", check=False)
        self.assertNotEqual(resultado.returncode, 0)
        self.assertIn("desconocido", resultado.stderr)

    def test_tarea_duplicada_se_rechaza_hasta_el_acuse(self):
        self.registrar("alfa", "beta")
        self.correr("send", "alfa", "beta", "--text", "hola", "--tarea", "unica")
        segundo = self.correr("send", "alfa", "beta", "--text", "hola otra vez",
                              "--tarea", "unica", check=False)
        self.assertNotEqual(segundo.returncode, 0)
        pendiente = json.loads(self.correr("inbox", "beta", "--json").stdout)[0]
        self.correr("ack", "beta", str(pendiente["id"]))
        tercero = self.correr("send", "alfa", "beta", "--text", "ahora si",
                              "--tarea", "unica", check=False)
        self.assertEqual(tercero.returncode, 0)

    def test_arriendo_choca_con_prefijo(self):
        self.registrar("alfa", "beta")
        self.correr("lease", "take", "alfa", "--repo", "/tmp/repo-prueba", "--path", "src")
        choque = self.correr("lease", "take", "beta", "--repo", "/tmp/repo-prueba",
                             "--path", "src/server.js", check=False)
        self.assertEqual(choque.returncode, 3)

    def test_arriendo_rechaza_ruta_absoluta(self):
        self.registrar("alfa")
        resultado = self.correr("lease", "take", "alfa", "--repo", "/tmp/repo-prueba",
                                "--path", "/etc/passwd", check=False)
        self.assertNotEqual(resultado.returncode, 0)
        self.assertIn("stay inside", resultado.stderr)

    def test_registro_vacio_no_trae_agentes_de_fabrica(self):
        listado = json.loads(self.correr("agent", "list", "--json").stdout)
        self.assertEqual(listado, [])


if __name__ == "__main__":
    unittest.main()
