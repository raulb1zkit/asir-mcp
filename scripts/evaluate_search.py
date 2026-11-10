#!/usr/bin/env python3
"""Compara recuperación con referencias privadas; no evalúa respuestas generadas."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

try:
    from .semantic_search import DEFAULT_DB, INDEX_PATH, MODEL_DIR, ROOT, Retriever, SearchFailure
    from .search_material import sha_file
except ImportError:
    from semantic_search import DEFAULT_DB, INDEX_PATH, MODEL_DIR, ROOT, Retriever, SearchFailure
    from search_material import sha_file


def first_relevant_rank(results, relevant_units):
    relevant = set(relevant_units)
    for rank, result in enumerate(results, 1):
        found = {s["source_unit_id"] for citation in result["citations"] for s in citation["segments"]}
        if relevant & found:
            return rank
    return None


def evaluate(questions_path, gold_path, output, database=DEFAULT_DB, model_dir=MODEL_DIR, index_path=INDEX_PATH, retriever_factory=None):
    questions = json.loads(questions_path.read_text(encoding="utf-8"))
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    if not questions["questions"] or len({q["id"] for q in questions["questions"]}) != len(questions["questions"]):
        raise SearchFailure("evaluation_questions_empty_or_duplicate")
    retriever = retriever_factory() if retriever_factory else Retriever(database, model_dir, index_path)
    try:
        if gold["corpus_sha256"] != retriever.engine.metadata["corpus_sha256"]:
            raise SearchFailure("evaluation_gold_is_stale")
        expected = {q["id"] for q in questions["questions"]}
        if set(gold["relevant_units"]) != expected or any(not v for v in gold["relevant_units"].values()):
            raise SearchFailure("evaluation_gold_incomplete")
        actual_units = {s["source_unit_id"] for c in retriever.engine.all_chunks() for cite in c["citations"] for s in cite["segments"]}
        if any(set(units) - actual_units for units in gold["relevant_units"].values()):
            raise SearchFailure("evaluation_gold_source_missing")
        start = time.monotonic()
        retriever.load_semantic()
        cold_seconds = time.monotonic() - start
        report = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(), "questions_sha256": sha_file(questions_path), "gold_sha256": sha_file(gold_path), "corpus_sha256": gold["corpus_sha256"], "model_fingerprint": retriever.encoder.fingerprint, "cold_retriever_load_seconds": round(cold_seconds, 4), "evaluation_type": "development_reference_passage_retrieval", "modes": {}, "out_of_scope_probes": []}
        for mode in ["lexical", "semantic", "hybrid"]:
            cases = []
            for question in questions["questions"]:
                start = time.monotonic()
                results = retriever.search(question["query"], mode=mode, limit=5)
                elapsed = time.monotonic() - start
                rank = first_relevant_rank(results, gold["relevant_units"][question["id"]])
                cases.append({"id": question["id"], "rank": rank, "milliseconds": round(elapsed * 1000, 3), "chunk_ids": [r["chunk_id"] for r in results]})
            summary = {"questions": len(cases), **{f"hits_at_{k}": sum(c["rank"] is not None and c["rank"] <= k for c in cases) for k in [1, 3, 5]}, "mrr_at_5": round(sum(1 / c["rank"] if c["rank"] else 0 for c in cases) / len(cases), 4), "mean_milliseconds": round(sum(c["milliseconds"] for c in cases) / len(cases), 3)}
            report["modes"][mode] = {"summary": summary, "cases": cases}
        for question in questions.get("out_of_scope_probes", []):
            modes = {}
            for mode in ["lexical", "semantic", "hybrid"]:
                results = retriever.search(question["query"], mode=mode, limit=5)
                modes[mode] = {"returned": len(results), "top_score": results[0]["score"] if results else None}
            report["out_of_scope_probes"].append({"id": question["id"], "results": modes, "answerability": "not_assessed"})
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({mode: value["summary"] for mode, value in report["modes"].items()}, ensure_ascii=False, indent=2))
        return report
    finally:
        retriever.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, default=ROOT / "evaluation/aso_questions.json")
    parser.add_argument("--gold", type=Path, default=ROOT / "data/aso/eval/gold.json")
    parser.add_argument("--output", type=Path, default=ROOT / "data/aso/eval/results.json")
    parser.add_argument("--backend", choices=["sqlite", "postgres"], default="sqlite")
    parser.add_argument("--database-config", type=Path, default=None)
    parser.add_argument("--model-dir", type=Path, default=MODEL_DIR)
    args = parser.parse_args()
    try:
        factory = None
        if args.backend == "postgres":
            try:
                from .postgres_search import PostgresRetriever
                from .supabase_db import service_config
            except ImportError:
                from postgres_search import PostgresRetriever
                from supabase_db import service_config
            config_path = args.database_config or (ROOT / ".private/asir-service-connection.json")
            factory = lambda: PostgresRetriever(service_config(config_path), args.model_dir)
        evaluate(args.questions, args.gold, args.output, model_dir=args.model_dir, retriever_factory=factory)
        return 0
    except (SearchFailure, OSError, ValueError, KeyError) as error:
        print(str(error) if isinstance(error, SearchFailure) else "No se ha podido evaluar la búsqueda.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
