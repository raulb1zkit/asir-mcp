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
CACHE_SCHEMA = 1
REVIEW_FLAGS = {"needs_ocr", "no_text_extracted", "unicode_replacement_character", "encoding_cp1252_assumed_requires_review", "list_numbering_unresolved", "tracked_changes_present", "alternate_content_unresolved", "textboxes_read_in_xml_order"}
STREAMS = ("native", "ocr_supplement", "visual_transcription")


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


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


def document_signature(entry, fields):
    """Representa solo el contenido que influye en el fragmentado, sin metadatos de fuente."""
    return {key: entry.get(key) for key in fields}


def document_cache_key(document, sha, supplements, annotations, config):
    """Identidad por documento: unidades, OCR, anotaciones y configuración del chunker."""
    ocr = sorted(
        (document_signature(entry, ("source_unit_id", "locator", "text", "text_sha256", "method", "warnings"))
         for (source, _), entry in supplements.items() if source == sha),
        key=lambda entry: str(entry.get("source_unit_id")),
    )
    visual = sorted(
        (document_signature(entry, ("source_unit_id", "image_member", "method", "text"))
         for (source, _), entry in annotations.items() if source == sha),
        key=lambda entry: str(entry.get("source_unit_id")),
    )
    payload = {
        "chunker_version": VERSION,
        "config": config,
        "source_sha256": sha,
        "document_warnings": document.get("warnings", []),
        "units": document["units"],
        "ocr": ocr,
        "annotations": visual,
    }
    return hashlib.sha256(canonical(payload)).hexdigest()


def document_streams(document, supplements, annotations):
    """Construye los flujos fragmentables y la cobertura esperada del documento."""
    sha = document["source"]["sha256"]
    streams = {method: [] for method in STREAMS}
    coverage = Counter()
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
    records = {(record["unit_id"], method): record for method, values in streams.items() for record in values}
    return streams, coverage, records


def chunk_document(document, supplements, annotations, target_chars, overlap_chars, subject):
    """Fragmenta un documento y devuelve sus chunks y la cobertura observada."""
    streams, coverage, _ = document_streams(document, supplements, annotations)
    chunks = []
    for method, records in streams.items():
        method_chunks = [chunk_group(document, group, method, target_chars, subject) for group in group_records(records, target_chars, overlap_chars)]
        for index, chunk in enumerate(method_chunks):
            for citation in chunk["citations"]:
                citation["previous_chunk_id"] = method_chunks[index - 1]["id"] if index and method_chunks[index - 1]["id"] != chunk["id"] else None
                citation["next_chunk_id"] = method_chunks[index + 1]["id"] if index + 1 < len(method_chunks) and method_chunks[index + 1]["id"] != chunk["id"] else None
        chunks.extend(method_chunks)
    return chunks, coverage


def cache_chunks(chunks):
    """Guarda los chunks sin metadatos de fuente para no acumular alias obsoletos."""
    cached = []
    for chunk in chunks:
        citation = chunk["citations"][0]
        cached.append({
            "id": chunk["id"],
            "text": chunk["text"],
            "text_sha256": chunk["text_sha256"],
            "characters": chunk["characters"],
            "warnings": chunk["warnings"],
            "requires_review": chunk["requires_review"],
            "citation": {
                "segments": citation["segments"],
                "method": citation["method"],
                "previous_chunk_id": citation["previous_chunk_id"],
                "next_chunk_id": citation["next_chunk_id"],
            },
        })
    return cached


def restore_chunks(cached, document, subject):
    """Reconstruye los chunks aplicando la procedencia actual del documento."""
    sources = [document["source"], *document.get("source_aliases", [])]
    chunks = []
    for item in cached["chunks"]:
        template = item["citation"]
        citations = [{
            "source": {**source, "subject": source.get("subject", subject)},
            "segments": template["segments"],
            "method": template["method"],
            "previous_chunk_id": template["previous_chunk_id"],
            "next_chunk_id": template["next_chunk_id"],
        } for source in sources]
        chunks.append({"schema_version": 1, "id": item["id"], "text": item["text"], "text_sha256": item["text_sha256"], "characters": item["characters"], "citations": citations, "warnings": item["warnings"], "requires_review": item["requires_review"]})
    return chunks


def valid_cache_entry(value, sha, key, document, supplements, annotations):
    """Valida estructura, identidad, contenido y cobertura antes de reutilizar."""
    if not isinstance(value, dict) or value.get("schema_version") != CACHE_SCHEMA or value.get("chunker_version") != VERSION:
        return False
    if value.get("source_sha256") != sha or value.get("key") != key:
        return False
    _, expected_coverage, records = document_streams(document, supplements, annotations)
    if value.get("coverage") != dict(expected_coverage):
        return False
    chunks = value.get("chunks")
    if not isinstance(chunks, list):
        return False
    found = set()
    for item in chunks:
        if not isinstance(item, dict):
            return False
        text = item.get("text")
        if not isinstance(text, str) or item.get("id") != "chunk:" + digest(text) or item.get("text_sha256") != digest(text) or item.get("characters") != len(text):
            return False
        if not isinstance(item.get("warnings"), list) or not all(isinstance(flag, str) for flag in item["warnings"]) or not isinstance(item.get("requires_review"), bool):
            return False
        citation = item.get("citation")
        if not isinstance(citation, dict) or citation.get("method") not in STREAMS:
            return False
        segments = citation.get("segments")
        if not isinstance(segments, list) or not segments:
            return False
        record_texts = []
        for segment in segments:
            if not isinstance(segment, dict) or not isinstance(segment.get("locator"), dict):
                return False
            if not all(isinstance(segment.get(field), str) for field in ("source_unit_id", "method", "quality")):
                return False
            start, end = segment.get("start"), segment.get("end")
            if not isinstance(start, int) or not isinstance(end, int) or isinstance(start, bool) or isinstance(end, bool) or not 0 <= start <= end <= len(text):
                return False
            record = records.get((segment["source_unit_id"], citation["method"]))
            if record is None or record["locator"] != segment["locator"] or record["method"] != segment["method"] or text[start:end] != record["text"]:
                return False
            record_texts.append(record["text"])
            found.add((segment["source_unit_id"], citation["method"]))
        if "\n\n".join(record_texts) != text:
            return False
        if any(citation.get(field) is not None and not isinstance(citation.get(field), str) for field in ("previous_chunk_id", "next_chunk_id")):
            return False
    return found == set(records)


def load_previous_cache(report_path):
    """Indexa cachés verificadas de un informe anterior; una entrada inválida es un miss."""
    if report_path is None:
        return {}
    try:
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = report.get("cache") if isinstance(report, dict) and report.get("complete") is True and not report.get("errors") else None
    if not isinstance(entries, dict):
        return {}
    root = Path(report_path).resolve().parent
    cache = {}
    for sha, entry in entries.items():
        if not isinstance(sha, str) or not isinstance(entry, dict):
            continue
        relative, expected = entry.get("path"), entry.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected, str):
            continue
        relative = Path(relative)
        if relative.is_absolute():
            continue
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or path == root:
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if hashlib.sha256(raw).hexdigest() != expected:
            continue
        try:
            cache[sha] = json.loads(raw)
        except ValueError:
            continue
    return cache


def store_cache(output, sha, value):
    relative = Path("cache") / (sha + ".json")
    dest = output / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(canonical(value))
    tmp.replace(dest)
    return {"path": relative.as_posix(), "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(), "key": value["key"]}


def run(text_report, ocr_report, output, target_chars=1800, overlap_chars=240, annotations_path=None, subject="ASO", reuse_report=None):
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
    config = {"subject": subject, "target_chars": target_chars, "overlap_chars": overlap_chars, "oversize_policy": "preserve_source_unit"}
    previous = load_previous_cache(reuse_report if reuse_report is not None else output / "report.json")
    raw_chunks, coverage = [], Counter()
    pending_cache = {}
    reused_documents = new_documents = 0
    for sha, document in documents.items():
        key = document_cache_key(document, sha, supplements, annotations, config)
        cached = previous.get(sha)
        if cached is not None and valid_cache_entry(cached, sha, key, document, supplements, annotations):
            chunks = restore_chunks(cached, document, subject)
            document_coverage = cached["coverage"]
            cache_value = cached
            reused_documents += 1
        else:
            chunks, document_coverage = chunk_document(document, supplements, annotations, target_chars, overlap_chars, subject)
            cache_value = {"schema_version": CACHE_SCHEMA, "chunker_version": VERSION, "source_sha256": sha, "key": key, "coverage": dict(document_coverage), "chunks": cache_chunks(chunks)}
            new_documents += 1
        coverage.update(document_coverage)
        raw_chunks.extend(chunks)
        pending_cache[sha] = cache_value
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
    cache_report = {sha: store_cache(output, sha, value) for sha, value in pending_cache.items()}
    report = {"schema_version": 1, "chunker_version": VERSION, "created_at": datetime.now(timezone.utc).isoformat(), "complete": True, "config": config, "inputs": {"native_sha256": hashlib.sha256(text_report.read_bytes()).hexdigest(), "ocr_sha256": hashlib.sha256(ocr_report.read_bytes()).hexdigest(), "annotations_sha256": hashlib.sha256(annotations_path.read_bytes()).hexdigest() if annotations_path and annotations_path.exists() else None}, "output_sha256": hashlib.sha256(dest.read_bytes()).hexdigest(), "cache": cache_report, "summary": {"documents": len(documents), "chunks_before_deduplication": len(raw_chunks), "chunks": len(merged), "duplicates_merged": len(raw_chunks) - len(merged), "requires_review": sum(c["requires_review"] for c in merged.values()), "oversize_chunks": sum(c["characters"] > target_chars for c in merged.values()), "reused_documents": reused_documents, "new_documents": new_documents, "coverage": dict(coverage)}}
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
    parser.add_argument("--reuse-report", type=Path, help="Reutiliza cachés íntegras de un informe anterior compatible.")
    args = parser.parse_args()
    try:
        return run(args.text_report, args.ocr_report, args.output, args.target_chars, args.overlap_chars, args.annotations, args.subject, args.reuse_report)
    except (ExtractionFailure, OSError, ValueError, KeyError, TypeError) as error:
        print(str(error) if isinstance(error, ExtractionFailure) else "No se han podido procesar los derivados locales.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
