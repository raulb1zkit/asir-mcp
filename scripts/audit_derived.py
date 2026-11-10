#!/usr/bin/env python3
"""Comprueba de forma independiente unidades nativas, OCR y segmentos citados."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

try:
    from .moodle_download import write_json
except ImportError:
    from moodle_download import write_json


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(root, output, archive=False):
    prefix = "archives-" if archive else ""
    native_path = root / (prefix + "extracted/all/report.json")
    ocr_path = root / (prefix + "ocr/all/report.json")
    chunks_path = root / (prefix + "chunks/chunks.jsonl")
    report_path = root / (prefix + "chunks/report.json")
    native = json.loads(native_path.read_text(encoding="utf-8"))
    ocr = json.loads(ocr_path.read_text(encoding="utf-8"))
    chunks_report = json.loads(report_path.read_text(encoding="utf-8"))
    if not all(r.get("complete") and not r.get("errors") for r in (native, ocr, chunks_report)):
        raise ValueError("input_incomplete")
    if ocr["source_report_sha256"] != file_hash(native_path) or chunks_report["output_sha256"] != file_hash(chunks_path):
        raise ValueError("stale_input")
    units = {}
    expected = set()
    for entry in native["documents"]:
        path = (native_path.parent / entry["path"]).resolve()
        if not path.is_relative_to(native_path.parent.resolve()) or file_hash(path) != entry["document_sha256"]:
            raise ValueError("native_document_integrity_failed")
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc["source"]["sha256"] != entry["source_sha256"]:
            raise ValueError("native_source_mismatch")
        for unit in doc["units"]:
            key = (entry["source_sha256"], unit["id"])
            if key in units:
                raise ValueError("duplicate_unit")
            units[key] = unit
            if unit["text"].strip():
                expected.add((*key, "native"))
    supplements = {}
    for entry in ocr["pages"]:
        path = (ocr_path.parent / entry["path"]).resolve()
        if not path.is_relative_to(ocr_path.parent.resolve()):
            raise ValueError("ocr_path_outside_root")
        page = json.loads(path.read_text(encoding="utf-8"))
        key = (entry["source_sha256"], entry["source_unit_id"])
        if key not in units or page["source_unit_id"] != key[1] or page["source"]["sha256"] != key[0] or hashlib.sha256(page["text"].encode()).hexdigest() != page["text_sha256"]:
            raise ValueError("ocr_integrity_failed")
        if any(page["locator"].get(name) != value for name, value in units[key]["locator"].items()):
            raise ValueError("ocr_locator_mismatch")
        base = units[key]["text"]
        known = " " + " ".join(base.split()) + " "
        if base.strip():
            text = "\n".join(line for line in page["text"].splitlines() if line.split() and " " + " ".join(line.split()) + " " not in known)
        else:
            text = page["text"].strip("\n")
        supplements[key] = text
        if text.strip():
            expected.add((*key, "ocr_supplement"))
    if any((unit.get("image_count", 0) > 0 or unit["kind"] == "image") and key not in supplements for key, unit in units.items()):
        raise ValueError("image_unit_without_ocr")
    found, counts = set(), Counter()
    with chunks_path.open(encoding="utf-8") as stream:
        for line in stream:
            chunk = json.loads(line)
            if hashlib.sha256(chunk["text"].encode()).hexdigest() != chunk["text_sha256"]:
                raise ValueError("chunk_hash_mismatch")
            counts["chunks"] += 1
            for citation in chunk["citations"]:
                for segment in citation["segments"]:
                    key = (citation["source"]["sha256"], segment["source_unit_id"])
                    if key not in units or units[key]["locator"] != segment["locator"]:
                        raise ValueError("segment_locator_mismatch")
                    method = citation["method"]
                    if method == "native":
                        unit = units[key]
                        text = (unit.get("list_label", "") + " " if unit.get("list_label") else "") + unit["text"]
                    elif method == "ocr_supplement":
                        text = supplements[key]
                    else:
                        raise ValueError("unknown_segment_method")
                    start, end = segment["start"], segment["end"]
                    if not 0 <= start <= end <= len(chunk["text"]) or chunk["text"][start:end] != text:
                        raise ValueError("segment_text_mismatch")
                    found.add((*key, method))
                    counts["segments"] += 1
    if found != expected:
        raise ValueError("unit_coverage_mismatch")
    report = {"complete": True, "root": str(root), "native_units": len(units), "expected_text_units": len(expected), "covered_text_units": len(found), "ocr_units": len(supplements), "skipped_formats": len(native.get("skipped", [])), **counts}
    write_json(output, report)
    print(json.dumps(report))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--archive", action="store_true")
    args = parser.parse_args()
    check(args.root, args.output, args.archive)


if __name__ == "__main__":
    main()
