#!/usr/bin/env python3
"""Comprueba el MCP remoto con token real y rechazo de acceso no autorizado."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import stat

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


def read_token(path):
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
        raise ValueError("token_file_must_be_regular_600_and_owned_by_user")
    token = Path(path).read_text(encoding="utf-8").strip()
    if not token or any(character.isspace() for character in token):
        raise ValueError("token_file_invalid")
    return token


async def unauthorized_status(url, token=None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx2.AsyncClient(timeout=30) as client:
        response = await client.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        return response.status_code


async def check(url, token, query, subject):
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx2.AsyncClient(headers=headers, timeout=60) as http:
        async with Client(streamable_http_client(url, http_client=http)) as client:
            tools = sorted(tool.name for tool in (await client.list_tools()).tools)
            catalog_resource = await client.read_resource("asir://catalogo")
            catalog = json.loads(catalog_resource.contents[0].text)
            found = await client.call_tool("buscar_material", {"consulta": query, "asignatura": subject, "limite": 3})
            if found.is_error or not found.structured_content or not found.structured_content.get("results"):
                raise ValueError("authorized_search_failed")
            first = found.structured_content["results"][0]
            read = await client.call_tool("leer_fragmento", {"fragmento_id": first["fragment_id"], "asignatura": subject, "vecinos": 0})
            if read.is_error or not read.structured_content:
                raise ValueError("authorized_read_failed")
            forbidden = await client.call_tool("buscar_material", {"consulta": query, "asignatura": "FOL"})
            if not forbidden.is_error:
                raise ValueError("subject_scope_not_enforced")
            return {
                "authorized": True,
                "tools": tools,
                "catalog_subjects": [item["name"] for item in catalog.get("subjects", [])],
                "catalog_transport": catalog.get("transport"),
                "results": len(found.structured_content["results"]),
                "first_fragment": first["fragment_id"],
                "references": len(first.get("references", [])),
                "read_characters": len(read.structured_content["fragment"]["text"]),
                "unauthorized_subject_rejected": True,
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080/mcp")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--query", default="Cómo cambiar la prioridad de un proceso")
    parser.add_argument("--subject", default="ASO")
    args = parser.parse_args()
    try:
        token = read_token(args.token_file)
        missing = asyncio.run(unauthorized_status(args.url))
        invalid = asyncio.run(unauthorized_status(args.url, "invalid-token"))
        report = asyncio.run(check(args.url, token, args.query, args.subject))
        report["missing_token_status"] = missing
        report["invalid_token_status"] = invalid
        if missing != 401 or invalid != 401:
            raise ValueError("unauthenticated_access_not_rejected")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        message = str(error) if isinstance(error, ValueError) else "remote_check_failed"
        print(json.dumps({"authorized": False, "error": message}, ensure_ascii=False), file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
