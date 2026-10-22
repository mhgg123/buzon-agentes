"""Carga qué comando ejecuta cada agente registrado y cómo leer su respuesta.

El despachador no sabe nada de ninguna herramienta en particular. Todo lo que hace
falta para correr "claude" o "codex" o cualquier otro agente de línea de comandos
vive en un archivo TOML (ver `ejemplos/agentes.toml.example`), no en este código.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import tomllib
from pathlib import Path


class ConfiguracionInvalida(RuntimeError):
    pass


FORMATOS_VALIDOS = ("texto", "archivo", "json_claude_code")


def _rutas_candidatas():
    desde_env = os.environ.get("BUZON_AGENTES_CONFIG")
    if desde_env:
        yield Path(desde_env).expanduser()
    yield Path("~/.config/buzon-agentes/agentes.toml").expanduser()
    yield Path.cwd() / "agentes.toml"


def cargar_configuracion():
    for ruta in _rutas_candidatas():
        if ruta.is_file():
            with ruta.open("rb") as archivo:
                datos = tomllib.load(archivo)
            return _validar(datos, ruta)
    raise ConfiguracionInvalida(
        "no se encontro agentes.toml. Defina BUZON_AGENTES_CONFIG=/ruta/agentes.toml "
        "o copie ejemplos/agentes.toml.example a ~/.config/buzon-agentes/agentes.toml"
    )


def _validar(datos, ruta):
    agentes = datos.get("agentes", {})
    if not agentes:
        raise ConfiguracionInvalida(f"{ruta}: no declara ningun [agentes.<nombre>]")
    for nombre, cfg in agentes.items():
        if not cfg.get("comando"):
            raise ConfiguracionInvalida(f"{ruta}: agentes.{nombre} no declara 'comando'")
        formato = cfg.get("formato", "texto")
        if formato not in FORMATOS_VALIDOS:
            raise ConfiguracionInvalida(
                f"{ruta}: agentes.{nombre}.formato={formato!r} no es uno de {FORMATOS_VALIDOS}"
            )
    revisores = datos.get("revision", {}).get("pares", {})
    for quien, revisa in revisores.items():
        if quien == revisa:
            raise ConfiguracionInvalida(
                f"{ruta}: revision.pares declara {quien!r}->{revisa!r}: un agente no "
                "puede ser su propio revisor"
            )
        if quien not in agentes or revisa not in agentes:
            raise ConfiguracionInvalida(
                f"{ruta}: revision.pares declara {quien!r}->{revisa!r} con un nombre no "
                "registrado en [agentes.*]"
            )
    return datos


def revisor_de(config, nombre):
    """El agente que revisa los commits de `nombre`, o None si no hay par configurado."""
    return config.get("revision", {}).get("pares", {}).get(nombre)


def ejecutar_agente_configurado(config, nombre, prompt, workdir, *, modo="lectura",
                                 timeout=120, entorno=None):
    """Corre el agente `nombre` con `prompt` y devuelve su respuesta como texto.

    `modo` selecciona entre `comando` (lectura) y `comando_escritura` (si el agente
    la declara); un agente sin `comando_escritura` no puede recibir tareas de
    escritura, y eso se decide aqui, no en el despachador.
    """
    cfg = config.get("agentes", {}).get(nombre)
    if cfg is None:
        raise RuntimeError(f"agente no configurado: {nombre}")
    clave_comando = "comando_escritura" if modo == "escritura" else "comando"
    comando = cfg.get(clave_comando) if modo == "escritura" else cfg.get("comando")
    if not comando:
        raise RuntimeError(f"agente {nombre!r} no declara {clave_comando!r} en agentes.toml")
    formato = cfg.get("formato", "texto")
    entorno_final = entorno if entorno is not None else os.environ.copy()

    if formato == "archivo":
        with tempfile.TemporaryDirectory(prefix=f"buzon-{nombre}-") as temp:
            archivo_salida = Path(temp) / "respuesta.txt"
            comando_final = [
                token.format(archivo_salida=str(archivo_salida), workdir=str(workdir))
                for token in comando
            ]
            resultado = subprocess.run(
                comando_final, input=prompt, text=True, capture_output=True,
                cwd=workdir, env=entorno_final, timeout=timeout, check=False,
            )
            if resultado.returncode:
                raise RuntimeError(f"{nombre} salio con codigo {resultado.returncode}: "
                                    f"{resultado.stderr.strip()[:300]}")
            if not archivo_salida.is_file():
                raise RuntimeError(f"{nombre} no escribio el archivo de salida esperado")
            return archivo_salida.read_text(encoding="utf-8")

    comando_final = [token.format(workdir=str(workdir)) for token in comando]
    resultado = subprocess.run(
        comando_final, input=prompt, text=True, capture_output=True,
        cwd=workdir, env=entorno_final, timeout=timeout, check=False,
    )
    if resultado.returncode:
        raise RuntimeError(f"{nombre} salio con codigo {resultado.returncode}: "
                            f"{resultado.stderr.strip()[:300]}")

    if formato == "json_claude_code":
        import json
        payload = json.loads(resultado.stdout)
        if payload.get("is_error") or payload.get("subtype") != "success":
            raise RuntimeError(f"{nombre} devolvio un resultado incompleto")
        return payload.get("result", "")

    return resultado.stdout
