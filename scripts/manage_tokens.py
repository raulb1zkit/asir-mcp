#!/usr/bin/env python3
"""Crea, lista y revoca tokens estáticos del MCP remoto.

El token en claro solo se imprime al crearlo. El archivo guarda su SHA-256.
"""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile

try:
    from .mcp_auth import HASH, IDENTIFIER, SCOPE, SUBJECT, TokenConfigError
except ImportError:
    from mcp_auth import HASH, IDENTIFIER, SCOPE, SUBJECT, TokenConfigError

DEFAULT_FILE = Path(os.environ.get("ASIR_TOKEN_FILE", str(Path.home() / ".config/asir/tokens.json")))


def load(path):
    if not path.exists():
        return {"version": 1, "tokens": []}
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
        raise TokenConfigError("token_file_must_be_regular_600_and_owned_by_user")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise TokenConfigError("token_file_invalid") from None
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("tokens"), list):
        raise TokenConfigError("token_file_invalid")
    return value


def save(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="tokens-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def split_values(value):
    return [item.strip() for item in value.split(",") if item.strip()]


def create(args):
    value = load(args.file)
    subjects = split_values(args.subjects)
    scopes = split_values(args.scopes)
    if not subjects or any(not SUBJECT.fullmatch(subject) for subject in subjects):
        raise TokenConfigError("subjects_invalid")
    if not scopes or any(not SCOPE.fullmatch(scope) for scope in scopes):
        raise TokenConfigError("scopes_invalid")
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    identifier = "tok_" + digest[:12]
    if any(record.get("id") == identifier for record in value["tokens"]):
        raise TokenConfigError("token_id_collision")
    expires = datetime.now(timezone.utc) + timedelta(days=args.days)
    record = {
        "id": identifier,
        "sha256": digest,
        "label": args.label,
        "subjects": subjects,
        "scopes": scopes,
        "requests_per_minute": args.rpm,
        "expires_at": expires.isoformat().replace("+00:00", "Z"),
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if not IDENTIFIER.fullmatch(identifier) or not HASH.fullmatch(digest):
        raise TokenConfigError("token_generation_failed")
    value["tokens"].append(record)
    save(args.file, value)
    return {**record, "token": token}


def list_tokens(args):
    value = load(args.file)
    return [
        {key: record.get(key) for key in ["id", "label", "subjects", "scopes", "requests_per_minute", "expires_at", "created_at"]}
        for record in value["tokens"]
    ]


def revoke(args):
    value = load(args.file)
    kept = [record for record in value["tokens"] if record.get("id") != args.id]
    if len(kept) == len(value["tokens"]):
        raise TokenConfigError("token_not_found")
    value["tokens"] = kept
    save(args.file, value)
    return {"revoked": args.id, "remaining": len(kept)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    sub = parser.add_subparsers(dest="command", required=True)
    create_parser = sub.add_parser("create")
    create_parser.add_argument("--label", required=True)
    create_parser.add_argument("--subjects", default="ASO")
    create_parser.add_argument("--scopes", default="asir:read")
    create_parser.add_argument("--rpm", type=int, default=30)
    create_parser.add_argument("--days", type=int, default=365)
    sub.add_parser("list")
    revoke_parser = sub.add_parser("revoke")
    revoke_parser.add_argument("--id", required=True)
    args = parser.parse_args()
    try:
        if args.command == "create":
            if not 1 <= args.rpm <= 600 or not 1 <= args.days <= 3650:
                raise TokenConfigError("invalid_create_arguments")
            result = create(args)
        elif args.command == "list":
            result = list_tokens(args)
        else:
            result = revoke(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (TokenConfigError, OSError, ValueError, KeyError) as error:
        message = str(error) if isinstance(error, TokenConfigError) else "token_operation_failed"
        parser.exit(1, json.dumps({"error": message}) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
