#!/usr/bin/env python3
"""Fragmenta el texto conservando unidades, procedencia y calidad de OCR."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

try:
    from .extract_text import ExtractionFailure, ROOT, save_json
except ImportError:
    from extract_text import ExtractionFailure, ROOT, save_json

VERSION = "1"
REVIEW_FLAGS = {"needs_ocr", "no_text_extracted", "unicode_replacement_character", "encoding_cp1252_assumed_requires_review", "list_numbering_unresolved", "tracked_changes_present", "alternate_content_unresolved", "textboxes_read_in_xml_order"}


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def safe_json(root, relative, expected_hash=None):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ExtractionFailure("derived_path_outside_root")
    raw = path.read_bytes()
    if expected_hash is not None and hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ExtractionFailure("native_document_hash_mismatch")
    return json.loads(raw.decode("utf-8"))


def supplemental_text(native, ocr):
    """Elimina solo líneas ya presentes literalmente, normalizando espacios."""
    if not native.strip():
        return ocr.strip("\n")
    known = " " + " ".join(native.split()) + " "
    novel = []
    for line in ocr.splitlines():
        normalized = " ".join(line.split())
        if normalized and (" " + normalized + " ") not in known:
            novel.append(line)
    return "\n".join(novel)


def group_records(records, target_chars=1800, overlap_chars=240):
    """No divide unidades: tablas y bloques de comandos permanecen enteros."""
    group = []
    for record in records:
        if group and (record["kind"] == "heading" or sum(len(r["text"]) for r in group) + 2 * len(group) + len(record["text"]) > target_chars):
            yield group
            overlap, size = [], 0
            budget = min(overlap_chars, max(0, target_chars - len(record["text"]) - 2))
            if record["kind"] != "heading":
                for previous in reversed(group):
                    extra = len(previous["text"]) + (2 if overlap else 0)
                    if size + extra > budget:
                        break
                    overlap.insert(0, previous)
                    size += extra
            group = overlap
        group.append(record)
        if len(record["text"]) > target_chars:
            yield group
            group = []
    if group:
        yield group


def chunk_group(document, records, method, target_chars, subject):
    text = "\n\n".join(r["text"] for r in records)
    segments, offset = [], 0
    for record in records:
        segments.append({"source_unit_id": record["unit_id"], "locator": record["locator"], "start": offset, "end": offset + len(record["text"]), "method": record["method"], "quality": record["quality"]})
        offset += len(record["text"]) + 2
    flags = set(document["warnings"])
    for record in records:
        flags.update(record["warnings"])
    if len(text) > target_chars:
        flags.add("oversized_source_unit_preserved")
    sources = [document["source"], *document.get("source_aliases", [])]
    citations = [{"source": {**source, "subject": source.get("subject", subject)}, "segments": segments, "method": method} for source in sources]
    return {"schema_version": 1, "id": "chunk:" + digest(text), "text": text, "text_sha256": digest(text), "characters": len(text), "citations": citations, "warnings": sorted(flags), "requires_review": bool(flags & REVIEW_FLAGS) or any(r["quality"] != "native" for r in records)}


def load_annotations(path, documents):
    result = {}
    if path is None or not path.exists():
        return result
    for entry in json.loads(path.read_text(encoding="utf-8"))["annotations"]:
        sha, unit_id = entry["source_sha256"], entry["source_unit_id"]
        document = documents.get(sha)
        unit = next((u for u in document["units"] if u["id"] == unit_id), None) if document else None
        if unit is None:
            raise ExtractionFailure("annotation_source_unknown")
        if unit["kind"] != "image" or unit.get("image_member") != entry.get("image_member"):
            raise ExtractionFailure("annotation_image_mismatch")
        if entry.get("method") != "visual_transcription" or not isinstance(entry.get("text"), str) or not entry["text"].strip():
            raise ExtractionFailure("invalid_annotation")
        if (sha, unit_id) in result:
            raise ExtractionFailure("duplicate_annotation")
        result[(sha, unit_id)] = entry
    return result


def run(text_report, ocr_report, output, target_chars=1800, overlap_chars=240, annotations_path=None, subject="ASO"):
    if target_chars < 200 or overlap_chars < 0 or overlap_chars >= target_chars:
        raise ExtractionFailure("invalid_chunk_sizes")
    native = json.loads(text_report.read_text(encoding="utf-8"))
    ocr = json.loads(ocr_report.read_text(encoding="utf-8"))
    if not native.get("complete") or not ocr.get("complete") or native.get("errors") or ocr.get("errors"):
        raise ExtractionFailure("incomplete_input_report")
    if hashlib.sha256(text_report.read_bytes()).hexdigest() != ocr["source_report_sha256"]:
        raise ExtractionFailure("ocr_report_is_stale")
    documents = {}
    for entry in native["documents"]:
        if not entry.get("document_sha256"):
            raise ExtractionFailure("native_document_hash_mismatch")
        doc = safe_json(text_report.parent, entry["path"], entry["document_sha256"])
        if doc["source"]["sha256"] != entry["source_sha256"]:
            raise ExtractionFailure("native_source_mismatch")
        documents[doc["source"]["sha256"]] = doc
    supplements = {}
    for entry in ocr["pages"]:
        doc = safe_json(ocr_report.parent, entry["path"])
        sha = doc["source"]["sha256"]
        unit_id = doc["source_unit_id"]
        source = documents.get(sha)
        unit = next((u for u in source["units"] if u["id"] == unit_id), None) if source else None
        if unit is None or entry["source_sha256"] != sha or entry.get("source_unit_id") != unit_id:
            raise ExtractionFailure("ocr_source_unknown")
        if digest(doc["text"]) != doc["text_sha256"]:
            raise ExtractionFailure("ocr_text_hash_mismatch")
        if "page" in unit["locator"] and unit["locator"]["page"] != doc["locator"].get("page"):
            raise ExtractionFailure("ocr_locator_mismatch")
        if unit["kind"] == "image" and (unit["image_member"] != doc["locator"].get("image") or unit["locator"]["block"] != doc["locator"].get("block")):
            raise ExtractionFailure("ocr_locator_mismatch")
        if (sha, unit_id) in supplements:
            raise ExtractionFailure("duplicate_ocr_result")
        supplements[(sha, unit_id)] = doc
    annotations = load_annotations(annotations_path, documents)
    raw_chunks, coverage = [], Counter()
    for sha, document in documents.items():
        streams = {"native": [], "ocr_supplement": [], "visual_transcription": []}
        for unit in document["units"]:
            coverage["source_units"] += 1
            text = unit["text"]
            if text.strip():
                coverage["native_text_units"] += 1
                rendered = (unit["list_label"] + " " if unit.get("list_label") else "") + text
                streams["native"].append({"unit_id": unit["id"], "locator": unit["locator"], "text": rendered, "kind": unit["kind"], "quality": "native", "method": "native", "warnings": unit["warnings"]})
            key = (sha, unit["id"])
            annotation = annotations.get(key)
            supplement = supplements.get(key)
            if annotation:
                coverage["visually_transcribed_units"] += 1
                streams["visual_transcription"].append({"unit_id": unit["id"], "locator": unit["locator"], "text": annotation["text"], "kind": "diagram", "quality": "visual_transcription", "method": "visual_transcription", "warnings": ["agent_visual_transcription_requires_review"]})
            elif supplement:
                coverage["ocr_units_processed"] += 1
                novel = supplemental_text(text, supplement["text"])
                if novel.strip():
                    coverage["ocr_units_with_supplemental_text"] += 1
                    streams["ocr_supplement"].append({"unit_id": unit["id"], "locator": unit["locator"], "text": novel, "kind": "ocr", "quality": "ocr_unreviewed", "method": supplement["method"], "warnings": supplement["warnings"]})
                else:
                    coverage["ocr_units_without_additional_text"] += 1
            elif unit.get("image_count", 0) > 0 or unit["kind"] == "image":
                coverage["image_units_without_ocr_or_annotation"] += 1
        for method, records in streams.items():
            chunks = [chunk_group(document, group, method, target_chars, subject) for group in group_records(records, target_chars, overlap_chars)]
            for index, chunk in enumerate(chunks):
                for citation in chunk["citations"]:
                    citation["previous_chunk_id"] = chunks[index - 1]["id"] if index and chunks[index - 1]["id"] != chunk["id"] else None
                    citation["next_chunk_id"] = chunks[index + 1]["id"] if index + 1 < len(chunks) and chunks[index + 1]["id"] != chunk["id"] else None
            raw_chunks.extend(chunks)
    merged = {}
    if coverage["image_units_without_ocr_or_annotation"]:
        raise ExtractionFailure("image_coverage_incomplete")
    for chunk in raw_chunks:
        if chunk["id"] not in merged:
            merged[chunk["id"]] = chunk
        else:
            prior = merged[chunk["id"]]
            prior["citations"].extend(chunk["citations"])
            prior["warnings"] = sorted(set(prior["warnings"] + chunk["warnings"]))
            prior["requires_review"] = prior["requires_review"] or chunk["requires_review"]
    output.mkdir(parents=True, exist_ok=True)
    dest = output / "chunks.jsonl"
    tmp = dest.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in merged.values()), encoding="utf-8")
    tmp.replace(dest)
    report = {"schema_version": 1, "chunker_version": VERSION, "created_at": datetime.now(timezone.utc).isoformat(), "complete": True, "config": {"subject": subject, "target_chars": target_chars, "overlap_chars": overlap_chars, "oversize_policy": "preserve_source_unit"}, "inputs": {"native_sha256": hashlib.sha256(text_report.read_bytes()).hexdigest(), "ocr_sha256": hashlib.sha256(ocr_report.read_bytes()).hexdigest(), "annotations_sha256": hashlib.sha256(annotations_path.read_bytes()).hexdigest() if annotations_path and annotations_path.exists() else None}, "output_sha256": hashlib.sha256(dest.read_bytes()).hexdigest(), "summary": {"documents": len(documents), "chunks_before_deduplication": len(raw_chunks), "chunks": len(merged), "duplicates_merged": len(raw_chunks) - len(merged), "requires_review": sum(c["requires_review"] for c in merged.values()), "oversize_chunks": sum(c["characters"] > target_chars for c in merged.values()), "coverage": dict(coverage)}}
    save_json(output / "report.json", report)
    print(json.dumps(report["summary"], ensure_ascii=False))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text-report", type=Path, default=ROOT / "data/aso/extracted/all/report.json")
    parser.add_argument("--ocr-report", type=Path, default=ROOT / "data/aso/ocr/all/report.json")
    parser.add_argument("--annotations", type=Path, default=ROOT / "data/aso/annotations/visual.json")
    parser.add_argument("--output", type=Path, default=ROOT / "data/aso/chunks")
    parser.add_argument("--target-chars", type=int, default=1800)
    parser.add_argument("--overlap-chars", type=int, default=240)
    parser.add_argument("--subject", default="ASO")
    args = parser.parse_args()
    try:
        return run(args.text_report, args.ocr_report, args.output, args.target_chars, args.overlap_chars, args.annotations, args.subject)
    except (ExtractionFailure, OSError, ValueError, KeyError, TypeError) as error:
        print(str(error) if isinstance(error, ExtractionFailure) else "No se han podido procesar los derivados locales.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
