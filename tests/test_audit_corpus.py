from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts.audit_corpus import AuditFailure, Paths, audit, main, sha_bytes, sha_text
from scripts.search_material import build_index


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def fixture(root):
    import numpy as np
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace

    paths = Paths(root)
    raw = b"original document fixture, not a real PDF"
    sha = sha_bytes(raw)
    relative = "objects/" + sha
    original = paths.moodle / relative
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(raw)
    source = {"sha256": sha, "path": relative, "url": "https://example.test/course.pdf", "title": "Material ficticio", "section": "UD1", "mime": "application/pdf", "filename": "course.pdf"}
    manifest = {"complete": True, "items": [{**source, "status": "downloaded", "bytes": len(raw)}]}
    save(paths.moodle / "manifest.json", manifest)
    units = [
        {"id": "u1", "kind": "page", "locator": {"page": 1}, "text": "cron programa tareas", "warnings": [], "image_count": 1},
        {"id": "u2", "kind": "page", "locator": {"page": 2}, "text": "configurar crontab", "list_label": "1.", "warnings": []},
        {"id": "u3", "kind": "image", "locator": {"block": 3}, "image_member": "Pictures/tree.png", "text": "", "warnings": []},
    ]
    document = {"source": source, "source_aliases": [], "units": units, "characters": sum(len(u["text"]) for u in units), "warnings": []}
    save(paths.native / "documents/doc.json", document)
    native_report = {"complete": True, "errors": [], "source_manifest_sha256": sha_bytes((paths.moodle / "manifest.json").read_bytes()), "documents": [{"source_sha256": sha, "path": "documents/doc.json", "document_sha256": sha_bytes((paths.native / "documents/doc.json").read_bytes()), "units": len(units), "characters": document["characters"]}]}
    save(paths.native / "report.json", native_report)
    config = {"languages": "spa+eng", "dpi": 180}
    recognized = units[0]["text"] + "\nimagen adicional"
    ocr_doc = {"source": source, "source_unit_id": "u1", "locator": {"page": 1}, "text": recognized, "text_sha256": sha_text(recognized), "config": config, "fingerprint": sha_text(json.dumps({"source_sha256": sha, "page": 1, "config": config}, sort_keys=True)), "method": "page-raster-ocr"}
    save(paths.ocr / "pages/page.json", ocr_doc)
    save(paths.ocr / "report.json", {"complete": True, "errors": [], "source_report_sha256": sha_bytes((paths.native / "report.json").read_bytes()), "config": config, "pages": [{"path": "pages/page.json", "source_sha256": sha, "source_unit_id": "u1", "characters": len(recognized)}]})
    annotation = {"source_sha256": sha, "source_unit_id": "u3", "image_member": "Pictures/tree.png", "method": "visual_transcription", "text": "raiz\n  rama"}
    save(paths.annotations, {"annotations": [annotation]})
    records = [("cron programa tareas", "u1", {"page": 1}, "native", "native", "native"), ("1. configurar crontab", "u2", {"page": 2}, "native", "native", "native"), ("imagen adicional", "u1", {"page": 1}, "ocr_supplement", "page-raster-ocr", "ocr_unreviewed"), (annotation["text"], "u3", {"block": 3}, "visual_transcription", "visual_transcription", "visual_transcription")]
    chunks = []
    for text, unit_id, locator, stream, method, quality in records:
        chunks.append({"id": "chunk:" + sha_text(text), "text": text, "text_sha256": sha_text(text), "characters": len(text), "requires_review": stream != "native", "warnings": [], "citations": [{"source": {**source, "subject": "ASO"}, "method": stream, "segments": [{"source_unit_id": unit_id, "locator": locator, "start": 0, "end": len(text), "method": method, "quality": quality}], "previous_chunk_id": None, "next_chunk_id": None}]})
    chunks[0]["citations"][0]["next_chunk_id"] = chunks[1]["id"]
    chunks[1]["citations"][0]["previous_chunk_id"] = chunks[0]["id"]
    report = {"complete": True, "config": {"subject": "ASO"}, "inputs": {"native_sha256": sha_bytes((paths.native / "report.json").read_bytes()), "ocr_sha256": sha_bytes((paths.ocr / "report.json").read_bytes()), "annotations_sha256": sha_bytes(paths.annotations.read_bytes())}, "summary": {"chunks": len(chunks), "documents": 1}}
    seal_chunks(paths, chunks, report)
    with redirect_stdout(io.StringIO()):
        build_index(paths.chunks / "chunks.jsonl", paths.chunks / "report.json", paths.search / "lexical.sqlite")
    tokenizer = Tokenizer(WordLevel({"[UNK]": 0, "passage": 1, "ASO": 2, "cron": 3, "crontab": 4}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = Whitespace()
    paths.model.mkdir(parents=True, exist_ok=True)
    tokenizer.save(str(paths.model / "tokenizer.json"))
    (paths.model / "model.onnx").write_bytes(b"fixture not executable")
    model = {"tokenizer_file": "tokenizer.json", "model_file": "model.onnx", "dimensions": 2, "max_tokens": 64, "files": [{"path": name, "sha256": sha_bytes((paths.model / name).read_bytes())} for name in ["tokenizer.json", "model.onnx"]]}
    save(paths.model / "model.json", model)
    windows = [{"chunk_id": c["id"], "char_start": 0, "char_end": len(c["text"]), "tokens": len(tokenizer.encode("passage: ASO | UD1 | Material ficticio\n" + c["text"]).ids)} for c in chunks]
    vectors = np.array([[1., 0.], [0., 1.], [-1., 0.], [0., -1.]], dtype=np.float32)
    semantic = {"schema_version": 1, "corpus_sha256": report["output_sha256"], "model_fingerprint": sha_text(json.dumps(model, sort_keys=True)), "dimensions": 2, "max_tokens": 64, "vectors_sha256": sha_bytes(vectors.tobytes()), "windows": windows, "summary": {"chunks": len(chunks), "windows": len(windows), "max_window_tokens": max(w["tokens"] for w in windows), "multiwindow_chunks": 0}}
    seal_semantic(paths, vectors, semantic)
    return paths, chunks, report


def seal_chunks(paths, chunks, report):
    paths.chunks.mkdir(parents=True, exist_ok=True)
    data = paths.chunks / "chunks.jsonl"
    data.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in chunks), encoding="utf-8")
    report["output_sha256"] = sha_bytes(data.read_bytes())
    report["summary"]["chunks"] = len(chunks)
    save(paths.chunks / "report.json", report)


def seal_semantic(paths, vectors, metadata):
    import numpy as np
    np.savez(paths.search / "semantic.npz", vectors=vectors, metadata=np.array(json.dumps(metadata)))
    save(paths.search / "semantic.json", metadata)


@unittest.skipUnless(importlib.util.find_spec("numpy") and importlib.util.find_spec("tokenizers"), "Audit fixtures require project venv")
class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths, self.chunks, self.report = fixture(Path(self.tmp.name))

    def test_complete_independent_audit_preserves_original_index(self):
        before = (self.paths.search / "lexical.sqlite").read_bytes()
        result = audit(self.paths)
        self.assertTrue(result["complete"])
        self.assertEqual(result["counts"]["source_text_records_covered"], 4)
        self.assertEqual(result["counts"]["image_units_covered"], 2)
        self.assertEqual(result["counts"]["semantic_windows"], 4)
        self.assertEqual((self.paths.search / "lexical.sqlite").read_bytes(), before)

    def test_original_corruption_is_detected(self):
        original = next((self.paths.moodle / "objects").iterdir())
        original.write_bytes(b"tampered")
        with self.assertRaisesRegex(AuditFailure, "original_hash_or_size"):
            audit(self.paths)

    def test_native_document_corruption_is_detected(self):
        path = self.paths.native / "documents/doc.json"
        path.write_text(path.read_text().replace("cron programa", "texto cambiado"))
        with self.assertRaisesRegex(AuditFailure, "native_document_hash"):
            audit(self.paths)

    def test_ocr_corruption_is_detected(self):
        path = self.paths.ocr / "pages/page.json"
        data = json.loads(path.read_text())
        data["text"] += "alterado"
        save(path, data)
        with self.assertRaisesRegex(AuditFailure, "ocr_text_hash_or_size"):
            audit(self.paths)

    def test_annotation_change_invalidates_chunk_chain(self):
        data = json.loads(self.paths.annotations.read_text())
        data["annotations"][0]["text"] = "otro arbol"
        save(self.paths.annotations, data)
        with self.assertRaisesRegex(AuditFailure, "chunk_annotation_is_stale"):
            audit(self.paths)

    def test_fully_rehashed_chunk_still_has_to_match_source_text(self):
        chunk = self.chunks[0]
        chunk["text"] = chunk["text"].replace("cron", "xxxx")
        chunk["text_sha256"] = sha_text(chunk["text"])
        chunk["id"] = "chunk:" + chunk["text_sha256"]
        seal_chunks(self.paths, self.chunks, self.report)
        with self.assertRaisesRegex(AuditFailure, "segment_text_mismatch"):
            audit(self.paths)

    def test_rehashed_corpus_cannot_omit_a_source_record(self):
        seal_chunks(self.paths, self.chunks[:-1], self.report)
        with self.assertRaisesRegex(AuditFailure, "source_text_coverage_incomplete"):
            audit(self.paths)

    def test_false_locators_are_detected_even_with_valid_text_hashes(self):
        self.chunks[0]["citations"][0]["segments"][0]["locator"] = {"page": 99}
        seal_chunks(self.paths, self.chunks, self.report)
        with self.assertRaisesRegex(AuditFailure, "segment_provenance_mismatch"):
            audit(self.paths)

    def test_cross_method_neighbors_are_detected(self):
        self.chunks[0]["citations"][0]["next_chunk_id"] = self.chunks[2]["id"]
        seal_chunks(self.paths, self.chunks, self.report)
        with self.assertRaisesRegex(AuditFailure, "neighbor_source_method_or_link"):
            audit(self.paths)

    def test_sqlite_payload_corruption_is_detected(self):
        with sqlite3.connect(self.paths.search / "lexical.sqlite") as db:
            db.execute("UPDATE chunks SET payload='{}' WHERE rowid=1")
        with self.assertRaisesRegex(AuditFailure, "sqlite_chunk_payload"):
            audit(self.paths)

    def test_fts_content_corruption_is_detected(self):
        with sqlite3.connect(self.paths.search / "lexical.sqlite") as db:
            db.execute("UPDATE passages SET body='alterado' WHERE rowid=1")
        with self.assertRaisesRegex(AuditFailure, "fts_content_mismatch"):
            audit(self.paths)

    def test_fts_inverted_index_corruption_is_detected_even_when_content_is_intact(self):
        with sqlite3.connect(self.paths.search / "lexical.sqlite") as db:
            db.execute("DELETE FROM passages_data WHERE id>10")
        with self.assertRaisesRegex(AuditFailure, "(sqlite|fts_internal)_integrity_failed"):
            audit(self.paths)

    def test_model_file_corruption_is_detected(self):
        (self.paths.model / "model.onnx").write_bytes(b"tampered model")
        with self.assertRaisesRegex(AuditFailure, "embedding_model_file_hash"):
            audit(self.paths)

    def test_vector_bit_corruption_is_detected(self):
        import numpy as np
        path = self.paths.search / "semantic.npz"
        with np.load(path, allow_pickle=False) as data:
            vectors = data["vectors"].copy()
            metadata = json.loads(str(data["metadata"]))
        vectors[0, 0] = .5
        seal_semantic(self.paths, vectors, metadata)
        with self.assertRaisesRegex(AuditFailure, "semantic_vector_hash_or_values"):
            audit(self.paths)

    def test_rehashed_missing_windows_are_detected(self):
        import numpy as np
        with np.load(self.paths.search / "semantic.npz", allow_pickle=False) as data:
            vectors = data["vectors"][:-1].copy()
            metadata = json.loads(str(data["metadata"]))
        metadata["windows"].pop()
        metadata["vectors_sha256"] = sha_bytes(vectors.tobytes())
        seal_semantic(self.paths, vectors, metadata)
        with self.assertRaisesRegex(AuditFailure, "semantic_chunk_coverage_incomplete"):
            audit(self.paths)

    def test_rehashed_forged_token_counts_are_detected(self):
        import numpy as np
        with np.load(self.paths.search / "semantic.npz", allow_pickle=False) as data:
            vectors = data["vectors"].copy()
            metadata = json.loads(str(data["metadata"]))
        metadata["windows"][0]["tokens"] += 1
        seal_semantic(self.paths, vectors, metadata)
        with self.assertRaisesRegex(AuditFailure, "semantic_window_tokens_mismatch"):
            audit(self.paths)

    def test_rehashed_window_gap_is_detected(self):
        import numpy as np
        from tokenizers import Tokenizer
        with np.load(self.paths.search / "semantic.npz", allow_pickle=False) as data:
            vectors = data["vectors"].copy()
            metadata = json.loads(str(data["metadata"]))
        first = metadata["windows"].pop(0)
        tokenizer = Tokenizer.from_file(str(self.paths.model / "tokenizer.json"))
        split = []
        for start, end in [(0, 10), (11, len(self.chunks[0]["text"]))]:
            tokens = len(tokenizer.encode("passage: ASO | UD1 | Material ficticio\n" + self.chunks[0]["text"][start:end]).ids)
            split.append({**first, "char_start": start, "char_end": end, "tokens": tokens})
        metadata["windows"] = split + metadata["windows"]
        vectors = np.concatenate([vectors[:1], vectors], axis=0)
        metadata["vectors_sha256"] = sha_bytes(vectors.tobytes())
        seal_semantic(self.paths, vectors, metadata)
        with self.assertRaisesRegex(AuditFailure, "semantic_window_gap_or_redundancy"):
            audit(self.paths)

    def test_reembedding_detects_rehashed_vector_permutation(self):
        import numpy as np
        with np.load(self.paths.search / "semantic.npz", allow_pickle=False) as data:
            correct = data["vectors"].copy()
            metadata = json.loads(str(data["metadata"]))
        permuted = correct[::-1].copy()
        metadata["vectors_sha256"] = sha_bytes(permuted.tobytes())
        seal_semantic(self.paths, permuted, metadata)
        class Encoder:
            def __init__(self, path):
                pass
            def encode(self, encodings):
                return correct
        with patch("scripts.semantic_search.LocalEncoder", Encoder):
            with self.assertRaisesRegex(AuditFailure, "semantic_reembedding_alignment_failed"):
                audit(self.paths, reembed_sample=4)

    def test_failed_npz_crc_check_replaces_previous_successful_report(self):
        output = self.paths.root / "data/aso/eval/audit.json"
        save(output, {"complete": True})
        npz = self.paths.search / "semantic.npz"
        raw = bytearray(npz.read_bytes())
        raw[raw.index(b"\x93NUMPY") + 128] ^= 1
        npz.write_bytes(raw)
        with patch("sys.argv", ["audit_corpus.py", "--root", str(self.paths.root)]), redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 1)
        result = json.loads(output.read_text())
        self.assertFalse(result["complete"])
        self.assertEqual(result["error_type"], "BadZipFile")

    def test_path_escape_is_rejected(self):
        manifest_path = self.paths.moodle / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["items"][0]["path"] = "../../../escape"
        save(manifest_path, manifest)
        native_path = self.paths.native / "report.json"
        native = json.loads(native_path.read_text())
        native["source_manifest_sha256"] = sha_bytes(manifest_path.read_bytes())
        save(native_path, native)
        ocr_path = self.paths.ocr / "report.json"
        ocr = json.loads(ocr_path.read_text())
        ocr["source_report_sha256"] = sha_bytes(native_path.read_bytes())
        save(ocr_path, ocr)
        with self.assertRaisesRegex(AuditFailure, "path_outside_artifact_root"):
            audit(self.paths)
