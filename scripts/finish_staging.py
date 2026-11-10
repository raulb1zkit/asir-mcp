#!/usr/bin/env python3
"""Continúa el trabajo al terminar embeddings y solo escribe en el ensayo privado."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time

try:
    from .moodle_download import write_json
    from .search_material import ROOT
except ImportError:
    from moodle_download import write_json
    from search_material import ROOT


def now():
    return datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--embedding-pid", type=int, required=True)
    args = parser.parse_args()
    out = ROOT / "data/all/eval"
    out.mkdir(parents=True, exist_ok=True)
    status = {"started_at": now(), "complete": False, "step": "waiting_for_embeddings", "failures": []}

    def save(step):
        status["step"] = step
        status["updated_at"] = now()
        write_json(out / "nightly-status.json", status)
        print(step, flush=True)

    save("waiting_for_embeddings")
    proc = Path("/proc") / str(args.embedding_pid)
    while proc.exists():
        try:
            command = (proc / "cmdline").read_bytes()
        except FileNotFoundError:
            break
        if not command:
            break
        if b"scripts/semantic_search.py" not in command or b"data/all/search/semantic.npz" not in command:
            status["failures"].append("embedding_pid_reused")
            save("failed")
            return 1
        time.sleep(15)
    semantic = ROOT / "data/all/search/semantic.npz"
    if not semantic.is_file():
        status["failures"].append("semantic_index_missing")
        save("failed")
        return 1
    save("importing_private_staging")
    command = [sys.executable, str(ROOT / "scripts/import_supabase.py"), "--staging", "--chunks", str(ROOT / "data/all/chunks/chunks.jsonl"), "--report", str(ROOT / "data/all/chunks/report.json"), "--semantic", str(semantic)]
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        status["failures"].append("staging_import_timeout")
        save("failed")
        return 1
    if result.returncode:
        status["failures"].append("staging_import_failed")
        (out / "staging-import-error.log").write_text(result.stderr[-4000:], encoding="utf-8")
        save("failed")
        return 1
    (out / "staging-import.json").write_text(result.stdout, encoding="utf-8")

    save("testing_staging_mcp")
    try:
        result = subprocess.run([sys.executable, str(ROOT / "scripts/check_staging_mcp.py")], cwd=ROOT, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        result = None
        status["failures"].append("staging_mcp_timeout")
    if result is None:
        pass
    elif result.returncode:
        status["failures"].append("staging_mcp_failed")
        (out / "staging-mcp-error.log").write_text(result.stderr[-4000:], encoding="utf-8")
    else:
        (out / "staging-mcp.json").write_text(result.stdout, encoding="utf-8")

    try:
        from .evaluate_rag import prepare
        from .postgres_search import PostgresRetriever
        from .semantic_search import Retriever, MODEL_DIR
        from .supabase_db import admin_config
    except ImportError:
        from evaluate_rag import prepare
        from postgres_search import PostgresRetriever
        from semantic_search import Retriever, MODEL_DIR
        from supabase_db import admin_config
    database = ROOT / "data/all/search/lexical.sqlite"
    factories = {
        "local": lambda: Retriever(database, MODEL_DIR, semantic),
        "staging": lambda: PostgresRetriever(admin_config(), MODEL_DIR, staging=True),
    }
    suites = {
        "new": (out / "questions.json", out / "gold.json"),
        "aso": (ROOT / "evaluation/aso_extended_questions.json", out / "aso_extended_gold.json"),
    }
    for backend, factory in factories.items():
        for name, (questions, gold) in suites.items():
            step = "evaluating_" + backend + "_" + name
            save(step)
            try:
                _, _, report = prepare(questions, gold, retriever_factory=factory)
                report.update({"complete": True, "backend": backend, "mcp_delivery_tested": False})
                write_json(out / (step + ".json"), report)
            except Exception as error:
                status["failures"].append(step + ":" + type(error).__name__)
    status["complete"] = not status["failures"]
    status["finished_at"] = now()
    save("finished" if status["complete"] else "finished_with_errors")
    return 0 if status["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
