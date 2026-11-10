#!/usr/bin/env python3
"""Guarda el token API de Cloudflare con entrada oculta, fuera de Git."""

import getpass
import os
from pathlib import Path
import sys


def main():
    if not sys.stdin.isatty():
        raise SystemExit("Ejecuta este comando en tu terminal interactiva.")
    token = getpass.getpass("Token API de Cloudflare (oculto): ").strip()
    if not token or any(ord(c) <= 32 or ord(c) == 127 for c in token):
        raise SystemExit("Token vacío o con caracteres de control; no se ha guardado.")
    path = Path(__file__).resolve().parents[1] / ".private/cloudflare-api-token"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit("Ya existe el token local; no se ha sobrescrito.")
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(token + "\n")
    print("Token guardado en .private/cloudflare-api-token con permisos 600.")


if __name__ == "__main__":
    main()
