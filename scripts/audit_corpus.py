#!/usr/bin/env python3
"""Audita el archivo local y sus índices sin red ni acceso a sesiones de Moodle.

La cobertura comprueba conservación y trazabilidad, no la exactitud semántica del
OCR ni que todo contenido de Moodle esté soportado por el descargador.
"""

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]


class AuditFailure(Exception):
    pass


def require(condition, code):
    if not condition:
        raise AuditFailure(code)


def sha_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha_text(value):
    return sha_bytes(value.encode("utf-8"))


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def local_path(root, relative):
    path = (root / relative).resolve()
    require(path.is_relative_to(root.resolve()), "path_outside_artifact_root")
    return path


@dataclass(frozen=True)
class Paths:
    root: Path = ROOT

    @property
    def moodle(self):
        return self.root / "data/aso/moodle"

    @property
    def native(self):
        return self.root / "data/aso/extracted/all"

    @property
    def ocr(self):
        return self.root / "data/aso/ocr/all"

    @property
    def annotations(self):
        return self.root / "data/aso/annotations/visual.json"

    @property
    def chunks(self):
        return self.root / "data/aso/chunks"

    @property
    def search(self):
        return self.root / "data/aso/search"

    @property
    def model(self):
        return self.root / "data/models/multilingual-e5-small"


def novel_ocr(native, recognized):
    # Independently express the documented line-level supplement policy.
    if not native.strip():
        return recognized.strip("\n")
    native_words = " " + " ".join(native.split()) + " "
    return "\n".join(line for line in recognized.splitlines()
                     if line.split() and " " + " ".join(line.split()) + " " not in native_words)


def load_sources(paths):
    manifest_path = paths.moodle / "manifest.json"
    native_path = paths.native / "report.json"
    ocr_path = paths.ocr / "report.json"
    manifest, native, ocr = map(read_json, [manifest_path, native_path, ocr_path])
    require(manifest.get("complete"), "incomplete_moodle_manifest")
    for report in [native, ocr]:
        require(report.get("complete") and not report.get("errors"), "incomplete_extraction_report")
    require(native["source_manifest_sha256"] == sha_bytes(manifest_path.read_bytes()), "native_manifest_is_stale")
    require(ocr["source_report_sha256"] == sha_bytes(native_path.read_bytes()), "ocr_report_is_stale")
    originals = {}
    downloaded = [item for item in manifest["items"] if item["status"] == "downloaded"]
    for item in downloaded:
        raw = local_path(paths.moodle, item["path"]).read_bytes()
        require(len(raw) == item["bytes"] and sha_bytes(raw) == item["sha256"], "original_hash_or_size_mismatch")
        originals.setdefault(item["sha256"], []).append(item)
    documents, units, records, images = {}, {}, {}, set()
    for entry in native["documents"]:
        path = local_path(paths.native, entry["path"])
        require(sha_bytes(path.read_bytes()) == entry["document_sha256"], "native_document_hash_mismatch")
        doc = read_json(path)
        source_sha = doc["source"]["sha256"]
        require(source_sha == entry["source_sha256"] and source_sha in originals, "native_source_missing")
        require(source_sha not in documents, "duplicate_native_document")
        require(doc["source"] in [{k: v for k, v in item.items() if k in doc["source"]} for item in originals[source_sha]], "native_source_metadata_mismatch")
        documents[source_sha] = doc
        require(len(doc["units"]) == entry["units"], "native_unit_count_mismatch")
        chars = sum(len(unit["text"]) for unit in doc["units"])
        require(chars == doc["characters"] == entry["characters"], "native_character_count_mismatch")
        for unit in doc["units"]:
            key = (source_sha, unit["id"])
            require(key not in units, "duplicate_source_unit")
            units[key] = unit
            if unit["text"].strip():
                text = (unit["list_label"] + " " if unit.get("list_label") else "") + unit["text"]
                records[(source_sha, unit["id"], "native")] = (text, "native", "native", unit["locator"])
            if unit.get("image_count", 0) > 0 or unit["kind"] == "image":
                images.add(key)
    require(set(documents) == set(originals), "originals_without_native_documents")
    recognized = {}
    for entry in ocr["pages"]:
        doc = read_json(local_path(paths.ocr, entry["path"]))
        key = (doc["source"]["sha256"], doc["source_unit_id"])
        require(key in units and key[0] == entry["source_sha256"] and key[1] == entry["source_unit_id"], "ocr_source_mismatch")
        require(key not in recognized, "duplicate_ocr_unit")
        require(doc["source"] == documents[key[0]]["source"], "ocr_source_metadata_mismatch")
        unit = units[key]
        require(all(doc["locator"].get(k) == v for k, v in unit["locator"].items()), "ocr_locator_mismatch")
        if unit["kind"] == "image":
            require(doc["locator"].get("image") == unit["image_member"], "ocr_image_mismatch")
        require(doc["text_sha256"] == sha_text(doc["text"]) and entry["characters"] == len(doc["text"]), "ocr_text_hash_or_size_mismatch")
        require(doc["config"] == ocr["config"], "ocr_configuration_mismatch")
        fingerprint = {"source_sha256": key[0], "page": doc["locator"].get("page"), "config": doc["config"]}
        if unit["kind"] == "image":
            fingerprint.update(image_member=unit["image_member"], unit_id=key[1])
        require(sha_text(json.dumps(fingerprint, sort_keys=True)) == doc["fingerprint"], "ocr_fingerprint_mismatch")
        recognized[key] = doc
    annotations = {}
    if paths.annotations.exists():
        for entry in read_json(paths.annotations)["annotations"]:
            key = (entry["source_sha256"], entry["source_unit_id"])
            require(key in units and key not in annotations, "annotation_source_missing_or_duplicate")
            require(units[key]["kind"] == "image" and units[key]["image_member"] == entry["image_member"], "annotation_image_mismatch")
            require(entry["method"] == "visual_transcription" and entry["text"].strip(), "annotation_invalid")
            annotations[key] = entry
    for key in images:
        require(key in recognized or key in annotations, "image_unit_without_ocr_or_annotation")
    for key, doc in recognized.items():
        if key in annotations:
            continue
        text = novel_ocr(units[key]["text"], doc["text"])
        if text.strip():
            records[(*key, "ocr_supplement")] = (text, doc["method"], "ocr_unreviewed", units[key]["locator"])
    for key, entry in annotations.items():
        records[(*key, "visual_transcription")] = (entry["text"], "visual_transcription", "visual_transcription", units[key]["locator"])
    counts = {"downloaded_objects": len(originals), "downloaded_manifest_entries": len(downloaded), "original_bytes": sum(local_path(paths.moodle, entries[0]["path"]).stat().st_size for entries in originals.values()), "native_units": len(units), "native_text_units": sum(key[2] == "native" for key in records), "ocr_units": len(recognized), "empty_ocr_units": sum(not doc["text"].strip() for doc in recognized.values()), "image_units": len(images), "image_units_covered": len(images & (recognized.keys() | annotations.keys())), "visual_annotations": len(annotations), "unsupported_moodle_items": sum(i["status"] == "unsupported" for i in manifest["items"])}
    return documents, records, counts


def verify_chunks(paths, documents, records):
    report_path = paths.chunks / "report.json"
    report = read_json(report_path)
    require(report.get("complete"), "chunk_report_incomplete")
    chunk_path = paths.chunks / "chunks.jsonl"
    require(report["output_sha256"] == sha_bytes(chunk_path.read_bytes()), "chunk_output_hash_mismatch")
    for key, path in [("native_sha256", paths.native / "report.json"), ("ocr_sha256", paths.ocr / "report.json")]:
        require(report["inputs"][key] == sha_bytes(path.read_bytes()), "chunk_input_is_stale")
    require(report["inputs"].get("annotations_sha256") == (sha_bytes(paths.annotations.read_bytes()) if paths.annotations.exists() else None), "chunk_annotation_is_stale")
    chunks, covered, segments = {}, set(), 0
    subject = report["config"]["subject"]
    for line in chunk_path.read_text(encoding="utf-8").splitlines():
        chunk = json.loads(line)
        chunk_id, text = chunk["id"], chunk["text"]
        require(chunk_id == "chunk:" + sha_text(text) and chunk["text_sha256"] == sha_text(text), "chunk_text_integrity_mismatch")
        require(chunk_id not in chunks and chunk["citations"] and chunk["characters"] == len(text), "duplicate_or_invalid_chunk")
        chunks[chunk_id] = chunk
        for citation in chunk["citations"]:
            source, method = citation["source"], citation["method"]
            require(source["sha256"] in documents, "citation_source_missing")
            document = documents[source["sha256"]]
            aliases = [document["source"], *document.get("source_aliases", [])]
            require(source in [{**s, "subject": s.get("subject", subject)} for s in aliases], "citation_metadata_mismatch")
            previous_end = 0
            require(citation["segments"], "citation_has_no_segments")
            for index, segment in enumerate(citation["segments"]):
                key = (source["sha256"], segment["source_unit_id"], method)
                require(key in records, "segment_source_missing")
                expected, extraction_method, quality, locator = records[key]
                start, end = segment["start"], segment["end"]
                require(type(start) is int and type(end) is int and 0 <= start < end <= len(text), "segment_range_invalid")
                require(start == (previous_end + 2 if index else 0) and (not index or text[previous_end:start] == "\n\n"), "segment_gap_or_overlap")
                require(text[start:end] == expected, "segment_text_mismatch")
                require(segment["locator"] == locator and segment["method"] == extraction_method and segment["quality"] == quality, "segment_provenance_mismatch")
                previous_end = end
                covered.add(key)
                segments += 1
                if quality != "native":
                    require(chunk["requires_review"], "unreviewed_ocr_without_review_flag")
            require(previous_end == len(text), "chunk_has_unattributed_text")
    require(covered == set(records), "source_text_coverage_incomplete")
    for chunk_id, chunk in chunks.items():
        for citation in chunk["citations"]:
            for key, reverse in [("previous_chunk_id", "next_chunk_id"), ("next_chunk_id", "previous_chunk_id")]:
                neighbor_id = citation.get(key)
                if neighbor_id is None:
                    continue
                require(neighbor_id in chunks and neighbor_id != chunk_id, "neighbor_missing_or_self")
                matching = [c for c in chunks[neighbor_id]["citations"] if c["source"] == citation["source"] and c["method"] == citation["method"]]
                require(matching and any(c.get(reverse) == chunk_id for c in matching), "neighbor_source_method_or_link_mismatch")
    require(report["summary"]["chunks"] == len(chunks) and report["summary"]["documents"] == len(documents), "chunk_summary_mismatch")
    return chunks, {"chunks": len(chunks), "segments_verified": segments, "source_text_records_covered": len(covered), "review_required_chunks": sum(c["requires_review"] for c in chunks.values())}


def verify_sqlite(paths, chunks):
    database = paths.search / "lexical.sqlite"
    db = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        metadata = dict(db.execute("SELECT key, value FROM metadata"))
        require(metadata["schema_version"] == "1" and metadata["corpus_sha256"] == sha_bytes((paths.chunks / "chunks.jsonl").read_bytes()) and metadata["chunk_report_sha256"] == sha_bytes((paths.chunks / "report.json").read_bytes()), "sqlite_index_is_stale")
        rows = list(db.execute("SELECT rowid,chunk_id,text,payload FROM chunks"))
        require(len(rows) == len(chunks) == int(metadata["chunks"]), "sqlite_chunk_count_mismatch")
        expected_citations = Counter()
        for rowid, chunk_id, text, payload in rows:
            require(chunk_id in chunks and text == chunks[chunk_id]["text"] and json.loads(payload) == chunks[chunk_id], "sqlite_chunk_payload_mismatch")
            chunk = chunks[chunk_id]
            titles, sections = [], []
            for citation in chunk["citations"]:
                source = citation["source"]
                titles.append(source["title"] + " " + source["filename"])
                sections.append(source["section"])
                expected_citations[(rowid, source["subject"], source["section"], source["title"], source["filename"], citation["method"])] += 1
            fts = db.execute("SELECT body,title,section FROM passages WHERE rowid=?", (rowid,)).fetchone()
            require(fts == (text, "\n".join(dict.fromkeys(titles)), "\n".join(dict.fromkeys(sections))), "fts_content_mismatch")
        require(db.execute("SELECT count(*) FROM passages").fetchone()[0] == len(chunks), "fts_row_count_mismatch")
        require(Counter(db.execute("SELECT * FROM citations")) == expected_citations, "sqlite_citation_table_mismatch")
        require(db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "sqlite_integrity_failed")
        # FTS integrity-check is a special INSERT. Run it on an in-memory backup,
        # so even the audit never writes to the original read-only index.
        copy = sqlite3.connect(":memory:")
        try:
            db.backup(copy)
            try:
                copy.execute("INSERT INTO passages(passages) VALUES('integrity-check')")
            except sqlite3.DatabaseError:
                raise AuditFailure("fts_internal_integrity_failed") from None
        finally:
            copy.close()
        return metadata
    finally:
        db.close()


def verify_semantic(paths, chunks, corpus_sha, reembed_sample=0):
    import numpy as np
    from tokenizers import Tokenizer

    manifest = read_json(paths.model / "model.json")
    verified_files = set()
    for entry in manifest["files"]:
        path = local_path(paths.model, entry["path"])
        require(sha_bytes(path.read_bytes()) == entry["sha256"], "embedding_model_file_hash_mismatch")
        verified_files.add(entry["path"])
    require({manifest["tokenizer_file"], manifest["model_file"]} <= verified_files, "embedding_model_assets_not_pinned")
    fingerprint = sha_text(json.dumps(manifest, sort_keys=True))
    tokenizer = Tokenizer.from_file(str(local_path(paths.model, manifest["tokenizer_file"])))
    tokenizer.no_padding()
    tokenizer.no_truncation()
    with np.load(paths.search / "semantic.npz", allow_pickle=False) as bundle:
        vectors = bundle["vectors"]
        metadata = json.loads(str(bundle["metadata"]))
    require(metadata["schema_version"] == 1 and metadata["corpus_sha256"] == corpus_sha and metadata["model_fingerprint"] == fingerprint, "semantic_index_is_stale")
    require(metadata == read_json(paths.search / "semantic.json"), "semantic_sidecar_mismatch")
    windows = metadata["windows"]
    require(vectors.dtype == np.float32 and vectors.shape == (len(windows), manifest["dimensions"]) and metadata["dimensions"] == manifest["dimensions"], "semantic_vector_shape_or_dtype_mismatch")
    require(np.isfinite(vectors).all() and sha_bytes(vectors.tobytes()) == metadata["vectors_sha256"], "semantic_vector_hash_or_values_mismatch")
    require(np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=5e-4), "semantic_vectors_not_normalized")
    require(metadata["max_tokens"] == manifest["max_tokens"], "semantic_token_limit_mismatch")
    grouped, encodings = {}, []
    for span in windows:
        require(span["chunk_id"] in chunks, "semantic_window_source_missing")
        chunk = chunks[span["chunk_id"]]
        text = chunk["text"]
        start, end = span["char_start"], span["char_end"]
        require(type(start) is int and type(end) is int and 0 <= start < end <= len(text), "semantic_window_range_invalid")
        source = chunk["citations"][0]["source"]
        prefix = f"passage: {source['subject']} | {source['section'][:120]} | {source['title'][:160]}\n"
        encoded = tokenizer.encode(prefix + text[start:end])
        require(len(encoded.ids) == span["tokens"] and 0 < len(encoded.ids) <= manifest["max_tokens"], "semantic_window_tokens_mismatch")
        encodings.append(encoded)
        grouped.setdefault(span["chunk_id"], []).append((start, end))
    require(set(grouped) == set(chunks), "semantic_chunk_coverage_incomplete")
    for chunk_id, spans in grouped.items():
        covered_end = 0
        for start, end in spans:
            require(start <= covered_end and end > covered_end, "semantic_window_gap_or_redundancy")
            covered_end = end
        require(covered_end == len(chunks[chunk_id]["text"]), "semantic_text_coverage_incomplete")
    summary = metadata["summary"]
    require(summary["chunks"] == len(chunks) and summary["windows"] == len(windows) and summary["max_window_tokens"] == max(w["tokens"] for w in windows) and summary["multiwindow_chunks"] == sum(len(spans) > 1 for spans in grouped.values()), "semantic_summary_mismatch")
    result = {"semantic_windows": len(windows), "multiwindow_chunks": summary["multiwindow_chunks"], "max_window_tokens": summary["max_window_tokens"], "model_files_verified": len(verified_files), "reembedded_windows": 0}
    if reembed_sample:
        try:
            from .semantic_search import LocalEncoder
        except ImportError:
            from semantic_search import LocalEncoder
        indices = np.linspace(0, len(windows) - 1, min(reembed_sample, len(windows)), dtype=int).tolist()
        encoder = LocalEncoder(paths.model)
        recomputed = encoder.encode([encodings[index] for index in indices])
        similarities = np.sum(recomputed * vectors[indices], axis=1)
        # Quantized inference may differ slightly with padding/batch composition.
        require(np.isfinite(similarities).all() and float(similarities.min()) >= 0.995, "semantic_reembedding_alignment_failed")
        result.update(reembedded_windows=len(indices), reembedding_min_cosine=float(similarities.min()), reembedding_mean_cosine=float(similarities.mean()), reembedding_minimum_accepted_cosine=0.995)
    return result


def audit(paths=None, reembed_sample=0):
    paths = paths or Paths()
    require(type(reembed_sample) is int and reembed_sample >= 0, "invalid_reembedding_sample_size")
    documents, records, source_counts = load_sources(paths)
    require(documents and all(any(key[0] == sha for key in records) for sha in documents), "source_without_indexable_text")
    chunks, chunk_counts = verify_chunks(paths, documents, records)
    metadata = verify_sqlite(paths, chunks)
    semantic_counts = verify_semantic(paths, chunks, metadata["corpus_sha256"], reembed_sample)
    return {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(), "complete": True, "corpus_sha256": metadata["corpus_sha256"], "checks": ["original_hashes_and_sizes", "upstream_report_hashes", "source_metadata_and_units", "native_ocr_annotation_text_coverage", "segment_text_offsets_and_locators", "neighbor_source_method_and_reciprocity", "sqlite_payloads_citations_and_fts", "sqlite_and_fts_internal_integrity", "model_asset_hashes", "semantic_vector_values_and_hash", "semantic_window_tokens_and_full_coverage", *(["semantic_reembedding_alignment"] if reembed_sample else [])], "counts": {**source_counts, **chunk_counts, **semantic_counts}, "limitations": ["Coverage is relative to downloaded objects; unsupported Moodle activities remain outside the corpus.", "Text conservation does not certify extraction or OCR accuracy against the original visual document.", "Index integrity does not measure retrieval relevance, answer correctness, authorization, or remote deployment security."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Project root, for a relocated local copy")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--reembed-sample", type=int, default=0, metavar="N", help="Recompute N evenly spaced embedding windows locally")
    args = parser.parse_args()
    output = args.output or args.root / "data/aso/eval/audit.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    def write_report(report):
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(output)

    # A killed or failed rerun must never leave a previous successful report
    # looking like the result of this run.
    write_report({"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(), "complete": False, "status": "running"})
    try:
        report = audit(Paths(args.root), args.reembed_sample)
    except Exception as error:
        code = str(error) if isinstance(error, AuditFailure) else "audit_input_or_runtime_error"
        report = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(), "complete": False, "error": code, "error_type": type(error).__name__}
    write_report(report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
