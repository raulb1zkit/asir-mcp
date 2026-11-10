#!/usr/bin/env python3
"""Guarda una conexión Supabase con entrada oculta y comprueba acceso de lectura."""

import argparse
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / ".private/supabase-connection.json"
EXPECTED_PROJECT = "kwvezxkhrjouaidzoglj"
CHECK_SQL = """BEGIN READ ONLY;
SET LOCAL statement_timeout = '10s';
SELECT json_build_object(
  'database', current_database(),
  'server_version', current_setting('server_version'),
  'read_only', current_setting('transaction_read_only'),
  'public_tables', (SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'),
  'pgvector_installed', EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'vector')
);
COMMIT;
"""


def parse_connection(value):
    try:
        uri = urlsplit(value)
        host, user = uri.hostname or "", unquote(uri.username or "")
        password, database = unquote(uri.password or ""), unquote(uri.path.removeprefix("/"))
        port = uri.port or 5432
        if uri.scheme not in {"postgres", "postgresql"} or uri.query or uri.fragment:
            raise ValueError
        if not re.fullmatch(r"[a-z0-9.-]+\.pooler\.supabase\.com", host):
            raise ValueError
        if user != "postgres." + EXPECTED_PROJECT or not password or database != "postgres" or port not in {5432, 6543}:
            raise ValueError
        if any(ord(c) < 32 or ord(c) == 127 for c in user + password + database):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("Conexión no válida para el proyecto ASIR-RAG esperado; revisa la URI y su codificación.") from None
    return {"project_ref": EXPECTED_PROJECT, "host": host, "port": port, "user": user, "password": password, "database": database}


def save_connection(config, path=PRIVATE):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(config, stream)
        stream.write("\n")


def load_connection(path=PRIVATE):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("La conexión debe ser un archivo propio con permisos 600.")
        config = json.load(stream)
    # Do not let modified config select arbitrary hosts or supply psql options.
    from urllib.parse import quote
    return parse_connection(f"postgresql://{quote(config['user'], safe='')}:{quote(config['password'], safe='')}@{config['host']}:{config['port']}/{config['database']}")


def check_connection(config, private_dir=PRIVATE.parent):
    cli = shutil.which("psql")
    if cli is None:
        raise ValueError("Necesitas el cliente psql para comprobar la conexión.")
    private_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix="supabase-pgpass-", dir=private_dir)
    try:
        def escape(value):
            return str(value).replace("\\", "\\\\").replace(":", "\\:")
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(":".join(escape(config[k]) for k in ["host", "port", "database", "user", "password"]) + "\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
        root_cert = private_dir / "supabase-ca.crt"
        env.update(PGPASSFILE=str(Path(filename).resolve()), PGSSLMODE="verify-full", PGSSLROOTCERT=str(root_cert.resolve()) if root_cert.exists() else "/etc/ssl/certs/ca-certificates.crt", PGCONNECT_TIMEOUT="15")
        args = [cli, "-X", "-w", "-qAt", "-v", "ON_ERROR_STOP=1", "-h", config["host"], "-p", str(config["port"]), "-U", config["user"], "-d", config["database"], "-c", CHECK_SQL]
        result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=30)
        if result.returncode:
            # Driver errors can quote connection details: never echo stderr.
            raise ValueError("No se pudo conectar: comprueba la cadena, red y certificado TLS. No se muestran credenciales ni errores del controlador.")
        report = json.loads(result.stdout)
        if report.get("read_only") != "on":
            raise ValueError("No se confirmó una transacción de solo lectura.")
        return {"project_ref": config["project_ref"], "tls": "verify-full", **report}
    finally:
        Path(filename).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Comprobar la conexión ya guardada sin volver a introducirla")
    args = parser.parse_args()
    try:
        if args.check:
            config = load_connection()
        else:
            if not os.isatty(0):
                raise ValueError("Ejecuta este comando en tu terminal interactiva.")
            config = parse_connection(getpass.getpass("URI PostgreSQL de ASIR-RAG (oculta): ").strip())
            save_connection(config)
            print("Conexión guardada en .private/supabase-connection.json con permisos 600.")
        print(json.dumps(check_connection(config), ensure_ascii=False, indent=2))
    except FileExistsError:
        parser.exit(1, "Ya hay una conexión guardada. Usa --check o elimina tú el archivo para renovarla.\n")
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        parser.exit(1, "No se pudo guardar o comprobar la conexión. Revisa el archivo local, la URI, los permisos y el acceso TLS. No se han mostrado secretos.\n")


if __name__ == "__main__":
    main()
