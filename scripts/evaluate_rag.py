#!/usr/bin/env python3
"""Evalúa referencias textuales fijadas y su entrega real por MCP, sin LLM."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from mcp import Client, StdioServerParameters

try:
    from .semantic_search import ROOT, Retriever, SearchFailure
    from .search_material import sha_file
except ImportError:
    from semantic_search import ROOT, Retriever, SearchFailure
    from search_material import sha_file


def evidence_in_chunk(chunk, evidence, text=None):
    """Evidence must belong to the cited unit and its exact text segment."""
    for ref in evidence:
        if not isinstance(ref.get("quote"), str) or not ref["quote"].strip():
            continue
        for citation in chunk["citations"]:
            if citation["source"]["sha256"] != ref["source_sha256"] or citation["method"] != ref["method"]:
                continue
            for span in citation["segments"]:
                if span["source_unit_id"] == ref["source_unit_id"] and span["locator"] == ref["locator"] and ref["quote"] in chunk["text"][span["start"]:span["end"]] and (text is None or ref["quote"] in text):
                    return True
    return False


def evidence_in_read(fragment, evidence, chunk):
    bounds = fragment["range"]
    start, end = bounds["start"], bounds["end"]
    if fragment["fragment_id"] != chunk["id"] or bounds["total"] != len(chunk["text"]) or not 0 <= start < end <= len(chunk["text"]) or fragment["text"] != chunk["text"][start:end]:
        return False
    for ref in evidence:
        if not isinstance(ref.get("quote"), str) or not ref["quote"].strip():
            continue
        # Bind the delivered bytes to the actual source segment, not just to a
        # compact page range which could cover a different occurrence of a word.
        bound = any(c["source"]["sha256"] == ref["source_sha256"] and c["method"] == ref["method"] and any(s["source_unit_id"] == ref["source_unit_id"] and s["locator"] == ref["locator"] and ref["quote"] in chunk["text"][max(start, s["start"]):min(end, s["end"])] for s in c["segments"]) for c in chunk["citations"])
        if not bound:
            continue
        for citation in fragment["references"]:
            if citation["source_id"] != ref["source_sha256"] or citation["method"] != ref["method"]:
                continue
            if any(kind in ref["locator"] and start <= ref["locator"][kind] <= end for location in citation["locations"] for kind, start, end in [(location["kind"], location["start"], location["end"])]):
                return True
    return False


def summary(cases):
    n = len(cases)
    if not n or any(c["rank"] is not None and (type(c["rank"]) is not int or c["rank"] < 1) for c in cases):
        raise SearchFailure("invalid_evaluation_ranks")
    return {"cases": n, **{f"hits_at_{k}": sum(c["rank"] is not None and c["rank"] <= k for c in cases) for k in [1, 3, 5]}, "mrr_at_5": round(sum(1/c["rank"] if c["rank"] is not None and c["rank"] <= 5 else 0 for c in cases) / n, 4)}


def prepare(questions_path, gold_path, retriever_factory=None):
    questions = json.loads(questions_path.read_text(encoding="utf-8"))
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    ids = [q["id"] for q in questions["questions"]]
    if not ids or len(set(ids)) != len(ids) or set(ids) != set(gold["evidence"]):
        raise SearchFailure("extended_evaluation_empty_duplicate_or_incomplete")
    retriever = retriever_factory() if retriever_factory else Retriever()
    try:
        if gold["corpus_sha256"] != retriever.engine.metadata["corpus_sha256"]:
            raise SearchFailure("extended_evaluation_gold_stale")
        chunks = {c["id"]: c for c in retriever.engine.all_chunks()}
        for refs in gold["evidence"].values():
            if not refs or any(not isinstance(ref.get("quote"), str) or not ref["quote"].strip() or not any(evidence_in_chunk(c, [ref]) for c in chunks.values()) for ref in refs):
                raise SearchFailure("extended_evidence_not_in_corpus")
        report = {"created_at": datetime.now(timezone.utc).isoformat(), "evaluation_type": "development_fixed_quote_retrieval_and_mcp_delivery", "teacher_validated": False, "relevance_labels_exhaustive": False, "corpus_sha256": gold["corpus_sha256"], "questions_sha256": sha_file(questions_path), "gold_sha256": sha_file(gold_path), "modes": {}, "out_of_scope_probes": []}
        for mode in ["lexical", "semantic", "hybrid"]:
            cases = []
            for q in questions["questions"]:
                rows = retriever.search(q["query"], mode=mode, limit=5, subject=q.get("subject", "ASO"))
                refs = gold["evidence"][q["id"]]
                rank = next((i for i, r in enumerate(rows, 1) if evidence_in_chunk(chunks[r["chunk_id"]], refs)), None)
                excerpt_rank = next((i for i, r in enumerate(rows, 1) if evidence_in_chunk(chunks[r["chunk_id"]], refs, r["excerpt"])), None)
                cases.append({"id": q["id"], "category": q["category"], "rank": rank, "exact_quote_in_excerpt_rank": excerpt_rank, "chunk_ids": [r["chunk_id"] for r in rows]})
            report["modes"][mode] = {"summary": summary(cases), "cases": cases}
        for q in questions.get("out_of_scope_probes", []):
            rows = retriever.search(q["query"], mode="hybrid", limit=5, subject="ASO")
            report["out_of_scope_probes"].append({"id": q["id"], "returned": len(rows), "answerability": "not_assessed"})
        return questions, gold, report
    finally:
        retriever.close()


async def verify_mcp(questions, gold):
    chunks = {c["id"]: c for c in map(json.loads, (ROOT / "data/aso/chunks/chunks.jsonl").read_text(encoding="utf-8").splitlines())}
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "scripts/mcp_server.py")], cwd=ROOT, env={"ASIR_ALLOWED_SUBJECTS": "ASO"})
    cases, reads = [], 0
    async with Client(params, read_timeout_seconds=30) as client:
        catalog = json.loads((await client.read_resource("asir://catalogo")).contents[0].text)
        for q in questions["questions"]:
            found = await client.call_tool("buscar_material", {"consulta": q["query"], "limite": 5})
            if found.is_error:
                raise SearchFailure("mcp_extended_search_failed")
            rows = found.structured_content["results"]
            rank, context_rank = None, None
            for i, row in enumerate(rows, 1):
                read = await client.call_tool("leer_fragmento", {"fragmento_id": row["fragment_id"], "vecinos": 1})
                reads += 1
                if read.is_error:
                    raise SearchFailure("mcp_extended_read_failed")
                fragment = read.structured_content["fragment"]
                if any(ref["subject"] != "ASO" for ref in fragment["references"]):
                    raise SearchFailure("mcp_scope_leak")
                if evidence_in_read(fragment, gold["evidence"][q["id"]], chunks[row["fragment_id"]]) and rank is None:
                    rank = i
                for neighbor in read.structured_content["context"]:
                    source_chunk = chunks[neighbor["fragment_id"]]
                    normalized = {**neighbor, "range": {"start": 0, "end": len(source_chunk["text"]), "total": len(source_chunk["text"])}}
                    if evidence_in_read(normalized, gold["evidence"][q["id"]], source_chunk) and context_rank is None:
                        context_rank = i
            cases.append({"id": q["id"], "category": q["category"], "rank": rank, "rank_with_context": min(r for r in [rank, context_rank] if r is not None) if rank is not None or context_rank is not None else None})
        # Every actual section is checked through the transport, including native-only.
        filter_checks, section_results = 0, []
        for subject in catalog["subjects"]:
            for unit in subject["units"]:
                found = await client.call_tool("buscar_material", {"consulta": "sistema procesos archivo", "unidad": unit, "solo_nativo": True, "limite": 8})
                if found.is_error:
                    raise SearchFailure("mcp_section_query_failed")
                if not found.structured_content["results"]:
                    raise SearchFailure("mcp_section_probe_empty")
                for row in found.structured_content["results"]:
                    if not row["references"] or any(ref["unit"] != unit or ref["method"] != "native" for ref in row["references"]):
                        raise SearchFailure("mcp_unit_or_method_leak")
                filter_checks += 1
                section_results.append({"unit": unit, "results": len(found.structured_content["results"])})
        errors = []
        for tool, arguments in [("buscar_material", {"consulta": "cron", "asignatura": "FOL"}), ("leer_fragmento", {"fragmento_id": "file:///etc/passwd"}), ("leer_fragmento", {"fragmento_id": "chunk:" + "0"*64}), ("buscar_material", {"consulta": "cron", "limite": 9})]:
            errors.append((await client.call_tool(tool, arguments)).is_error)
        if not all(errors):
            raise SearchFailure("mcp_negative_case_not_rejected")
    return {"summary": summary(cases), "with_context_summary": summary([{"rank": c["rank_with_context"]} for c in cases]), "cases": cases, "read_calls": reads, "section_filter_checks": filter_checks, "section_results": section_results, "negative_cases_rejected": len(errors)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, default=ROOT / "evaluation/aso_extended_questions.json")
    parser.add_argument("--gold", type=Path, default=ROOT / "data/aso/eval/extended_gold.json")
    parser.add_argument("--output", type=Path, default=ROOT / "data/aso/eval/extended_results.json")
    parser.add_argument("--backend", choices=["sqlite", "postgres"], default="sqlite")
    parser.add_argument("--database-config", type=Path, default=None)
    parser.add_argument("--model-dir", type=Path, default=ROOT / "data/models/multilingual-e5-small")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def save_report(value):
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(args.output)
    save_report({"complete": False, "status": "running", "created_at": datetime.now(timezone.utc).isoformat()})
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
        questions, gold, report = prepare(args.questions, args.gold, retriever_factory=factory)
        report["backend"] = args.backend
        report["mcp_hybrid"] = asyncio.run(verify_mcp(questions, gold))
        report["complete"] = True  # Evaluation completed; misses remain in metrics.
        save_report(report)
        print(json.dumps({**{m: v["summary"] for m, v in report["modes"].items()}, "mcp_hybrid": report["mcp_hybrid"]["summary"], "mcp_with_context": report["mcp_hybrid"]["with_context_summary"]}, indent=2))
    except (SearchFailure, OSError, ValueError, KeyError) as error:
        save_report({"complete": False, "error": str(error) if isinstance(error, SearchFailure) else "evaluation_input_or_runtime_error"})
        parser.exit(1, f"Evaluation failed: {error}\n")


if __name__ == "__main__":
    main()
