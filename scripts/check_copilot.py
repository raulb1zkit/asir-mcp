#!/usr/bin/env python3
"""Prueba opcional con Copilot: envía unos pocos pasajes de ASO a su proveedor.

Requiere sesión de Copilot y consume su cuota. Guarda la traza fuera de Git.
--summarize permite comprobar una traza existente sin volver a llamar al modelo.
"""

import argparse
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
TOOLS = {"asir-buscar_material", "asir-leer_fragmento"}
PROMPT = """Prueba del servidor MCP ASIR. Usa exclusivamente asir-buscar_material y
asir-leer_fragmento. Primero busca en ASO cómo cambiar la prioridad de un proceso
que ya se está ejecutando (limite 2, modo hybrid). Después lee el primer fragmento
encontrado (vecinos 0). Responde en español en menos de 120 palabras, basándote
en ese texto y citando el nombre del documento y la página. Conserva los avisos
de calidad. No uses conocimiento externo ni otras herramientas. No escribas archivos."""


def summarize(path):
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue  # CLI diagnostics can accompany JSONL on stderr.
    starts = [e["data"] for e in events if e.get("type") == "tool.execution_start"]
    completed = {e["data"]["toolCallId"]: e["data"] for e in events if e.get("type") == "tool.execution_complete"}
    final = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if final is None or final.get("exitCode") != 0 or len(starts) != 2 or {s["toolName"] for s in starts} != TOOLS:
        raise ValueError("Host test did not finish with exactly the two expected tools")
    if not all(completed.get(s["toolCallId"], {}).get("success") is True for s in starts):
        raise ValueError("A host tool call failed")
    if starts[0]["toolName"] != "asir-buscar_material" or starts[1]["toolName"] != "asir-leer_fragmento":
        raise ValueError("Expected search followed by read")
    search = completed[starts[0]["toolCallId"]]["result"]["structuredContent"]
    read = completed[starts[1]["toolCallId"]]["result"]["structuredContent"]
    requested = starts[1]["arguments"]["fragmento_id"]
    if not search["results"] or requested != search["results"][0]["fragment_id"] or read["fragment"]["fragment_id"] != requested:
        raise ValueError("Read did not follow the first search result")
    refs = read["fragment"]["references"]
    messages = [e["data"].get("content", "") for e in events if e.get("type") == "assistant.message"]
    answer = next((m for m in reversed(messages) if m), "")
    if not refs or not any(ref["filename"] in answer for ref in refs):
        raise ValueError("Answer does not name a returned document")
    model = next((e["data"]["chosenModel"] for e in events if e.get("type") == "session.auto_mode_resolved"), starts[0].get("model"))
    return {"model": model, "transport": "stdio", "successful_tools": [s["toolName"] for s in starts], "read_matches_first_result": True, "answer_names_document": True, "references": [{"filename": ref["filename"], "locations": ref["locations"]} for ref in refs], "usage_reported_by_cli": final.get("usage"), "automatic_answer_quality_evaluation": False}


def run(output, prompt=PROMPT):
    cli = shutil.which("copilot")
    if cli is None:
        raise ValueError("Copilot CLI is not installed")
    output.mkdir(parents=True, exist_ok=True)
    config = {"mcpServers": {"asir": {"type": "stdio", "command": str(ROOT / ".venv/bin/python"), "args": [str(ROOT / "scripts/mcp_server.py")], "cwd": str(ROOT), "tools": ["buscar_material", "leer_fragmento"], "env": {"ASIR_ALLOWED_SUBJECTS": "ASO"}, "deferTools": "never", "disableToolCache": True}}}
    config_path = output / "copilot-config.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    # No shell: paths and prompt stay literal arguments. No persistent MCP config.
    args = [cli, "-C", str(ROOT), "--trust-folder", "--silent", "--no-auto-update", "--no-custom-instructions", "--no-ask-user", "--disable-builtin-mcps", "--sync-session=off", "--log-dir", str(output / "copilot-logs"), "--available-tools=asir-buscar_material,asir-leer_fragmento", "--allow-tool=asir(buscar_material)", "--allow-tool=asir(leer_fragmento)", "--additional-mcp-config", "@" + str(config_path), "--output-format=json", "-p", prompt]
    trace = output / "copilot-host.jsonl"
    with trace.open("w", encoding="utf-8") as stream:
        process = subprocess.run(args, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, timeout=180)
    if process.returncode:
        raise ValueError("Copilot failed; inspect the local trace")
    return trace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/aso/mcp")
    parser.add_argument("--summarize", type=Path, help="Read an existing trace without contacting the provider")
    args = parser.parse_args()
    try:
        result = summarize(args.summarize or run(args.output_dir))
    except (ValueError, KeyError, OSError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"Host check failed: {error}\n")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "copilot-check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
