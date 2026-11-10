#!/usr/bin/env python3
"""Combina fragmentos comprobados conservando todas sus citas y procedencias."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

try:
    from .extract_text import save_json
except ImportError:
    from extract_text import save_json


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def merge(inputs, output):
    merged = {}
    reports = []
    total = 0
    for directory in inputs:
        report_path = directory / "report.json"
        chunks_path = directory / "chunks.jsonl"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if not report.get("complete") or report.get("output_sha256") != digest(chunks_path):
            raise ValueError("incomplete_or_stale_input")
        reports.append({"directory": str(directory), "report_sha256": digest(report_path), "chunks_sha256": digest(chunks_path)})
        with chunks_path.open(encoding="utf-8") as stream:
            for line in stream:
                chunk = json.loads(line)
                sha = hashlib.sha256(chunk["text"].encode()).hexdigest()
                if chunk["id"] != "chunk:" + sha or chunk["text_sha256"] != sha or not chunk["citations"]:
                    raise ValueError("invalid_chunk")
                total += 1
                previous = merged.get(chunk["id"])
                if previous is None:
                    merged[chunk["id"]] = chunk
                else:
                    previous["citations"].extend(chunk["citations"])
                    previous["warnings"] = sorted(set(previous["warnings"] + chunk["warnings"]))
                    previous["requires_review"] |= chunk["requires_review"]
    output.mkdir(parents=True, exist_ok=True)
    destination = output / "chunks.jsonl"
    temporary = destination.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for chunk in merged.values():
            stream.write(json.dumps(chunk, ensure_ascii=False) + "\n")
    temporary.replace(destination)
    report = {"schema_version": 1, "chunker_version": "merge-1", "created_at": datetime.now(timezone.utc).isoformat(), "complete": True, "inputs": reports, "output_sha256": digest(destination), "summary": {"chunks": len(merged), "input_chunks": total, "duplicates_merged": total - len(merged), "citations": sum(len(x["citations"]) for x in merged.values())}}
    save_json(output / "report.json", report)
    print(json.dumps(report["summary"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    merge(args.input, args.output)


if __name__ == "__main__":
    main()
