#!/usr/bin/env python3
"""Guarda una cookie de Moodle localmente sin mostrarla ni usar argumentos."""

import getpass
import json
import os
from pathlib import Path
import re
import sys


def main():
    if not sys.stdin.isatty():
        raise SystemExit("Ejecuta este comando en tu terminal interactiva.")
    name = input("Nombre de cookie [MoodleSession]: ").strip() or "MoodleSession"
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise SystemExit("Nombre de cookie no válido.")
    value = getpass.getpass("Valor de la cookie (oculto): ").strip()
    if not re.fullmatch(r"[A-Za-z0-9%._~+/-]+", value):
        raise SystemExit("Valor no válido: pega solo el valor de la cookie.")
    path = Path(__file__).resolve().parents[1] / ".private/moodle-session.json"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    # O_EXCL evita sobreescribir una sesión existente o seguir un enlace simbólico.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit("Ya existe .private/moodle-session.json; bórralo tú para renovar la sesión.")
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump({"name": name, "value": value}, file)
        file.write("\n")
    print("Sesión guardada en .private/moodle-session.json. El valor no se ha mostrado.")


if __name__ == "__main__":
    main()
