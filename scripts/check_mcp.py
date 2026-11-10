#!/usr/bin/env python3
"""Prueba de protocolo con el SDK cliente y un servidor stdio como subproceso."""

import argparse
import asyncio
import importlib.metadata
import json
from pathlib import Path
import sys

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]


async def check(mode="auto", environment=None):
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "scripts/mcp_server.py")], cwd=ROOT, env=environment or {"ASIR_ALLOWED_SUBJECTS": "ASO", "ORT_DISABLE_TELEMETRY": "1"})
    async with Client(params, mode=mode, read_timeout_seconds=30) as client:
        listed = await client.list_tools()
        names = {tool.name for tool in listed.tools}
        assert names == {"buscar_material", "leer_fragmento"}, names
        for tool in listed.tools:
            assert tool.annotations.read_only_hint is True
        catalog = await client.read_resource("asir://catalogo")
        data = json.loads(catalog.contents[0].text)
        assert any(s["name"] == "ASO" for s in data["subjects"])
        found = await client.call_tool("buscar_material", {"consulta": "Cómo cambiar la prioridad de un proceso que ya está ejecutándose", "limite": 3})
        assert not found.is_error, found
        results = found.structured_content["results"]
        assert results and results[0]["references"]
        fragment = await client.call_tool("leer_fragmento", {"fragmento_id": results[0]["fragment_id"], "vecinos": 1})
        assert not fragment.is_error, fragment
        assert fragment.structured_content["fragment"]["text"]
        assert fragment.structured_content["fragment"]["references"]
        rejected = await client.call_tool("leer_fragmento", {"fragmento_id": "../../.private/moodle-session.json"})
        assert rejected.is_error
        denied = await client.call_tool("buscar_material", {"consulta": "crontab", "asignatura": "NO_PERMITIDA"})
        assert denied.is_error
        oversized = await client.call_tool("buscar_material", {"consulta": "crontab", "limite": 999})
        assert oversized.is_error
        return {"sdk": importlib.metadata.version("mcp"), "protocol": client.protocol_version, "tools": sorted(names), "catalog_subjects": [s["name"] for s in data["subjects"]], "search_results": len(results), "read_characters": len(fragment.structured_content["fragment"]["text"]), "context_chunks": len(fragment.structured_content["context"]), "invalid_path_rejected": rejected.is_error, "unavailable_subject_rejected": denied.is_error, "oversized_limit_rejected": oversized.is_error}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["auto", "legacy"], default="auto")
    parser.add_argument("--backend", choices=["sqlite", "postgres"], default="sqlite")
    parser.add_argument("--database-config", type=Path, default=ROOT / ".private/asir-service-connection.json")
    parser.add_argument("--model-dir", type=Path, default=ROOT / "data/models/multilingual-e5-small")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    environment = {
        "ASIR_ALLOWED_SUBJECTS": "ASO",
        "ORT_DISABLE_TELEMETRY": "1",
        "ASIR_BACKEND": args.backend,
    }
    if args.backend == "postgres":
        environment["ASIR_DATABASE_CONFIG"] = str(args.database_config)
        environment["ASIR_MODEL_DIR"] = str(args.model_dir)
    result = asyncio.run(check(args.mode, environment))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
