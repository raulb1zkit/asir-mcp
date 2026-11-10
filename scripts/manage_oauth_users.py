#!/usr/bin/env python3
"""Gestiona la lista privada de usuarios OAuth autorizados por UUID."""

import argparse
import json
import os
from pathlib import Path
from contextlib import contextmanager
import fcntl
import stat
import sys

try:
    from .manage_tokens import save, split_values
    from .mcp_auth import SUBJECT, TokenConfigError, _read_private
    from .oauth_mcp import validate_users, _uuid
except ImportError:
    from manage_tokens import save, split_values
    from mcp_auth import SUBJECT, TokenConfigError, _read_private
    from oauth_mcp import validate_users, _uuid


def initialize(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise TokenConfigError("oauth_user_file_exists") from None
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump({"version": 1, "users": []}, stream)
        stream.write("\n")
    return {"initialized": True, "users": 0}


def load(path):
    value, _ = _read_private(path)
    validate_users(value)
    return value


@contextmanager
def locked(path):
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path.with_name(path.name + ".lock"), flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
            raise TokenConfigError("oauth_lock_file_invalid")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def update(path, identifier, subjects=None):
    identifier = _uuid(identifier)
    if not identifier:
        raise TokenConfigError("oauth_user_id_invalid")
    with locked(path):
        value = load(path)
        remaining = [user for user in value["users"] if user["id"] != identifier]
        if subjects is None:
            if len(remaining) == len(value["users"]):
                raise TokenConfigError("oauth_user_not_found")
        else:
            if not subjects or any(not isinstance(s, str) or not SUBJECT.fullmatch(s) for s in subjects):
                raise TokenConfigError("subjects_invalid")
            remaining.append({"id": identifier, "subjects": list(dict.fromkeys(subjects))})
        value["users"] = remaining
        save(path, value)
        return {"updated": True, "users": len(remaining)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=Path(os.environ.get("ASIR_OAUTH_USERS_FILE", "/etc/asir/oauth-users.json")))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("list")
    grant = sub.add_parser("grant")
    grant.add_argument("--id", required=True)
    grant.add_argument("--subjects", required=True)
    revoke = sub.add_parser("revoke")
    revoke.add_argument("--id", required=True)
    args = parser.parse_args()
    try:
        if args.command == "init":
            result = initialize(args.file)
        elif args.command == "list":
            result = load(args.file)["users"]
        else:
            result = update(args.file, args.id, split_values(args.subjects) if args.command == "grant" else None)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (TokenConfigError, OSError) as error:
        print(str(error) if isinstance(error, TokenConfigError) else "oauth_user_file_error", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
