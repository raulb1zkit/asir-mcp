#!/usr/bin/env python3
"""Prueba el protocolo MCP en memoria contra el esquema privado de ensayo."""

import asyncio
import json
import sys
import time

from mcp import Client

try:
    from .material_service import Settings
    from .mcp_server import create_server
    from .postgres_search import PostgresRetriever
    from .supabase_db import admin_config
except ImportError:
    from material_service import Settings
    from mcp_server import create_server
    from postgres_search import PostgresRetriever
    from supabase_db import admin_config


CASES = {
    "ASO": "Cómo cambiar la prioridad de un proceso",
    "Bases de Datos": "Cómo desactivar un trigger en SQL Server",
    "SAD": "Qué diferencia hay entre cifrado simétrico y asimétrico",
    "IAW": "Cómo conectar PHP a MySQL usando PDO",
    "Digitalizacion": "Qué es la computación en la nube",
}


async def run():
    settings = Settings(backend="postgres", allowed_subjects=frozenset(CASES), transport="in_memory_staging")
    config = admin_config()
    server = create_server(settings, retriever_factory=lambda: PostgresRetriever(config, settings.model_dir, staging=True))
    results = []
    async with Client(server, read_timeout_seconds=90) as client:
        catalog = json.loads((await client.read_resource("asir://catalogo")).contents[0].text)
        subjects = {item["name"] for item in catalog["subjects"]}
        if subjects != set(CASES):
            raise ValueError("staging_catalog_subjects_mismatch")
        for subject, query in CASES.items():
            started = time.monotonic()
            found = await client.call_tool("buscar_material", {"consulta": query, "asignatura": subject, "limite": 3})
            if found.is_error or not found.structured_content.get("results"):
                raise ValueError("staging_search_failed:" + subject)
            first = found.structured_content["results"][0]
            if not first.get("references") or any(ref["subject"] != subject for ref in first["references"]):
                raise ValueError("staging_reference_scope_mismatch:" + subject)
            read = await client.call_tool("leer_fragmento", {"fragmento_id": first["fragment_id"], "asignatura": subject, "vecinos": 0})
            if read.is_error or not read.structured_content.get("fragment", {}).get("text"):
                raise ValueError("staging_read_failed:" + subject)
            results.append({"subject": subject, "results": len(found.structured_content["results"]), "seconds": round(time.monotonic() - started, 3)})
        forbidden = await client.call_tool("buscar_material", {"consulta": "prueba", "asignatura": "FOL"})
        if not forbidden.is_error:
            raise ValueError("staging_forbidden_subject_not_rejected")
    return {"complete": True, "mode": "in_memory_mcp_private_staging", "public_endpoint_tested": False, "subjects": results, "forbidden_subject_rejected": True}


def main():
    try:
        print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(json.dumps({"complete": False, "error": str(error) if isinstance(error, ValueError) else type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
