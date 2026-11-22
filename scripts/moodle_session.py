#!/usr/bin/env python3
"""Guarda una cookie de Moodle localmente sin mostrarla ni usar argumentos."""

import argparse
import getpass
import json
import os
from pathlib import Path
import re
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", action="store_true", help="Guarda usuario y contraseña para renovación web.")
    parser.add_argument("--path", type=Path, help="Ruta privada alternativa.")
    parser.add_argument("--replace", action="store_true", help="Sustituye el secreto existente de forma atómica.")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        raise SystemExit("Ejecuta este comando en tu terminal interactiva.")
    path = args.path or Path(__file__).resolve().parents[1] / (".private/moodle-credentials.json" if args.credentials else ".private/moodle-session.json")
    if path.exists() and not args.replace:
        raise SystemExit("El archivo ya existe; usa --replace para renovarlo.")
    if args.credentials:
        username = input("Usuario Moodle: ").strip()
        password = getpass.getpass("Contraseña Moodle (oculta): ")
        if not username or not password:
            raise SystemExit("Credenciales incompletas.")
        try:
            from .moodle_auth import save_private
        except ImportError:
            from moodle_auth import save_private
        save_private(path, {"username": username, "password": password})
        print("Credenciales guardadas de forma privada; no se han mostrado.")
        return
    name = input("Nombre de cookie [MoodleSession]: ").strip() or "MoodleSession"
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise SystemExit("Nombre de cookie no válido.")
    value = getpass.getpass("Valor de la cookie (oculto): ").strip()
    if not re.fullmatch(r"[A-Za-z0-9%._~+/-]+", value):
        raise SystemExit("Valor no válido: pega solo el valor de la cookie.")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.replace:
        try:
            from .moodle_auth import save_private
        except ImportError:
            from moodle_auth import save_private
        save_private(path, {"name": name, "value": value})
        print("Sesión renovada de forma privada; el valor no se ha mostrado.")
        return
    # O_EXCL evita sobreescribir una sesión existente o seguir un enlace simbólico.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit("El archivo ya existe; usa --replace para renovarlo.")
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump({"name": name, "value": value}, file)
        file.write("\n")
    print("Sesión guardada de forma privada. El valor no se ha mostrado.")


if __name__ == "__main__":
    main()
