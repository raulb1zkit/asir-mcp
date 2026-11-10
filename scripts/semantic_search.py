#!/usr/bin/env python3
"""Embeddings E5 locales con ventanas completas y fusión de rangos RRF."""

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sys
import time

try:
    from .search_material import DEFAULT_DB, ROOT, SearchEngine, SearchFailure, sha_file
except ImportError:
    from search_material import DEFAULT_DB, ROOT, SearchEngine, SearchFailure, sha_file

MODEL_DIR = ROOT / "data/models/multilingual-e5-small"
INDEX_PATH = ROOT / "data/aso/search/semantic.npz"


def validate_coverage(windows, text_length, max_tokens=512):
    if not windows or windows[0]["char_start"] != 0 or windows[-1]["char_end"] != text_length:
        raise SearchFailure("incomplete_embedding_coverage")
    previous_end = 0
    for span in windows:
        if not 0 <= span["char_start"] < span["char_end"] <= text_length or span["char_start"] > previous_end or not 0 < span["tokens"] <= max_tokens:
            raise SearchFailure("invalid_embedding_window")
        previous_end = max(previous_end, span["char_end"])


def encode_windows(tokenizer, text, prefix, max_tokens=512, overlap_tokens=64):
    """Ventanas por offsets: cada una repite el prefijo E5 y conserva su rango."""
    # Tokenizar de una vez archivos de código o índices minificados de varios
    # megabytes puede tener un coste desproporcionado. Subdividir por caracteres
    # conserva todo el texto y evita entradas gigantes en el tokenizador.
    if len(text) > 8192:
        result = []
        start = 0
        while start < len(text):
            end = min(len(text), start + 4096)
            for encoding, span in encode_windows(tokenizer, text[start:end], prefix, max_tokens, overlap_tokens):
                result.append((encoding, {**span, "char_start": span["char_start"] + start, "char_end": span["char_end"] + start}))
            if end == len(text):
                break
            start = end - 256
        validate_coverage([span for _, span in result], len(text), max_tokens)
        return result
    full = tokenizer.encode(prefix + text)
    if len(full.ids) <= max_tokens:
        result = [(full, {"char_start": 0, "char_end": len(text), "tokens": len(full.ids)})]
        validate_coverage([span for _, span in result], len(text), max_tokens)
        return result
    raw = tokenizer.encode(text, add_special_tokens=False)
    budget = max_tokens - len(tokenizer.encode(prefix).ids) - 8
    if budget <= overlap_tokens:
        raise SearchFailure("embedding_context_too_long")
    result, start = [], 0
    while start < len(raw.ids):
        end = min(len(raw.ids), start + budget)
        char_start = 0 if start == 0 else raw.offsets[start][0]
        while end > start:
            char_end = len(text) if end == len(raw.ids) else raw.offsets[end - 1][1]
            encoding = tokenizer.encode(prefix + text[char_start:char_end])
            if len(encoding.ids) <= max_tokens:
                break
            end -= max(1, len(encoding.ids) - max_tokens)
        if end <= start or char_end <= char_start:
            raise SearchFailure("embedding_window_failed")
        result.append((encoding, {"char_start": char_start, "char_end": char_end, "tokens": len(encoding.ids)}))
        if end == len(raw.ids):
            break
        start = max(start + 1, end - overlap_tokens)
    validate_coverage([span for _, span in result], len(text), max_tokens)
    return result


def mean_pool(hidden, mask):
    import numpy as np
    expanded = mask[..., None].astype(np.float32)
    pooled = (hidden * expanded).sum(axis=1) / expanded.sum(axis=1).clip(min=1)
    norms = np.linalg.norm(pooled, axis=1, keepdims=True)
    if not np.isfinite(pooled).all() or (norms <= 0).any():
        raise SearchFailure("invalid_embedding_values")
    return (pooled / norms).astype(np.float32)


class LocalEncoder:
    def __init__(self, directory=MODEL_DIR, threads=2):
        os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError:
            raise SearchFailure("semantic_dependencies_missing_use_project_venv") from None
        self.np = np
        ort.disable_telemetry_events()
        self.manifest = json.loads((directory / "model.json").read_text(encoding="utf-8"))
        for entry in self.manifest["files"]:
            path = (directory / entry["path"]).resolve()
            if not path.is_relative_to(directory.resolve()) or sha_file(path) != entry["sha256"]:
                raise SearchFailure("embedding_model_hash_mismatch")
        self.fingerprint = hashlib.sha256(json.dumps(self.manifest, sort_keys=True).encode()).hexdigest()
        self.tokenizer = Tokenizer.from_file(str(directory / self.manifest["tokenizer_file"]))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.pad_id = self.tokenizer.token_to_id("<pad>")
        if self.pad_id is None:
            raise SearchFailure("unknown_padding_token")
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(directory / self.manifest["model_file"]), sess_options=options, providers=["CPUExecutionProvider"])
        self.input_names = {i.name for i in self.session.get_inputs()}

    def encode(self, encodings, batch_size=8):
        np = self.np
        if not encodings or batch_size < 1:
            raise SearchFailure("empty_embedding_batch")
        order = sorted(range(len(encodings)), key=lambda i: len(encodings[i].ids))
        vectors = np.zeros((len(encodings), self.manifest["dimensions"]), dtype=np.float32)
        for offset in range(0, len(order), batch_size):
            indices = order[offset:offset + batch_size]
            width = max(len(encodings[i].ids) for i in indices)
            if width > self.manifest["max_tokens"]:
                raise SearchFailure("embedding_input_exceeds_token_limit")
            ids = np.full((len(indices), width), self.pad_id, dtype=np.int64)
            mask = np.zeros_like(ids)
            types = np.zeros_like(ids)
            for row, index in enumerate(indices):
                encoded = encodings[index]
                ids[row, :len(encoded.ids)] = encoded.ids
                mask[row, :len(encoded.ids)] = encoded.attention_mask
                types[row, :len(encoded.ids)] = encoded.type_ids
            inputs = {"input_ids": ids, "attention_mask": mask, "token_type_ids": types}
            hidden = self.session.run(["last_hidden_state"], {k: v for k, v in inputs.items() if k in self.input_names})[0]
            vectors[indices] = mean_pool(hidden, mask)
            if len(encodings) > 16 and (offset + len(indices) == len(order) or (offset + len(indices)) % 64 == 0):
                print(f"Embeddings locales: {offset + len(indices)}/{len(order)} ventanas", file=sys.stderr, flush=True)
        return vectors

    def query(self, text):
        if not text.strip() or len(text) > 2000:
            raise SearchFailure("query_empty_or_too_long")
        encoding = self.tokenizer.encode("query: " + text)
        if len(encoding.ids) > self.manifest["max_tokens"]:
            raise SearchFailure("query_exceeds_embedding_limit")
        return self.encode([encoding])[0]


def build_semantic(database=DEFAULT_DB, model_dir=MODEL_DIR, destination=INDEX_PATH):
    start = time.monotonic()
    engine = SearchEngine(database)
    encodings, windows = [], []
    try:
        chunks = engine.all_chunks()
        if not chunks:
            raise SearchFailure("empty_corpus_for_semantic_index")
        encoder = LocalEncoder(model_dir)
        for chunk_index, chunk in enumerate(chunks, 1):
            source = chunk["citations"][0]["source"]
            prefix = "passage: " + source["subject"] + " | " + source["section"][:120] + " | " + source["title"][:160] + "\n"
            for encoding, span in encode_windows(encoder.tokenizer, chunk["text"], prefix, encoder.manifest["max_tokens"]):
                encodings.append(encoding)
                windows.append({"chunk_id": chunk["id"], **span})
            if chunk_index % 500 == 0:
                print(f"Tokenización local: {chunk_index}/{len(chunks)} fragmentos, {len(windows)} ventanas", file=sys.stderr, flush=True)
        vectors = encoder.encode(encodings)
        window_counts = Counter(w["chunk_id"] for w in windows)
        metadata = {"schema_version": 1, "corpus_sha256": engine.metadata["corpus_sha256"], "model_fingerprint": encoder.fingerprint, "model_repository": encoder.manifest["repository"], "model_revision": encoder.manifest["revision"], "dimensions": vectors.shape[1], "max_tokens": encoder.manifest["max_tokens"], "overlap_tokens": 64, "context": "subject-section-title", "vectors_sha256": hashlib.sha256(vectors.tobytes()).hexdigest(), "windows": windows, "summary": {"chunks": len(chunks), "windows": len(windows), "multiwindow_chunks": sum(count > 1 for count in window_counts.values()), "max_window_tokens": max(w["tokens"] for w in windows), "seconds": round(time.monotonic() - start, 3)}}
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_suffix(".tmp")
        with tmp.open("wb") as file:
            encoder.np.savez(file, vectors=vectors, metadata=encoder.np.array(json.dumps(metadata)))
        tmp.replace(destination)
        destination.with_suffix(".json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({**metadata["summary"], "bytes": destination.stat().st_size, "index": str(destination)}))
    finally:
        engine.close()


class Retriever:
    def __init__(self, database=DEFAULT_DB, model_dir=MODEL_DIR, index_path=INDEX_PATH):
        self.engine = SearchEngine(database)
        self.model_dir, self.index_path = model_dir, index_path
        self.encoder = None
        self.vectors, self.metadata = None, None

    def close(self):
        self.engine.close()

    def load_semantic(self):
        if self.encoder is not None:
            return
        encoder = LocalEncoder(self.model_dir)
        np = encoder.np
        with np.load(self.index_path, allow_pickle=False) as bundle:
            vectors = bundle["vectors"]
            metadata = json.loads(str(bundle["metadata"]))
        m = metadata
        if m.get("schema_version") != 1 or m["corpus_sha256"] != self.engine.metadata["corpus_sha256"] or m["model_fingerprint"] != encoder.fingerprint:
            raise SearchFailure("semantic_index_is_stale_or_incompatible")
        if vectors.shape != (len(m["windows"]), m["dimensions"]) or not np.isfinite(vectors).all() or hashlib.sha256(vectors.tobytes()).hexdigest() != m["vectors_sha256"]:
            raise SearchFailure("semantic_vectors_invalid")
        if m["dimensions"] != encoder.manifest["dimensions"] or not np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=5e-4):
            raise SearchFailure("semantic_vectors_not_normalized")
        chunks = {c["id"]: c for c in self.engine.all_chunks()}
        grouped = {}
        for window in m["windows"]:
            if window["chunk_id"] not in chunks:
                raise SearchFailure("semantic_window_reference_unknown")
            grouped.setdefault(window["chunk_id"], []).append(window)
        if set(grouped) != set(chunks):
            raise SearchFailure("semantic_index_coverage_incomplete")
        for chunk_id, spans in grouped.items():
            validate_coverage(spans, len(chunks[chunk_id]["text"]), encoder.manifest["max_tokens"])
        self.encoder, self.vectors, self.metadata = encoder, vectors, metadata

    def semantic(self, query, limit=5, subject=None, section=None, native_only=False):
        if not 1 <= limit <= 100:
            raise SearchFailure("invalid_result_limit")
        self.load_semantic()
        vector = self.encoder.query(query)
        similarities = self.vectors @ vector
        best = {}
        for index, score in enumerate(similarities):
            window = self.metadata["windows"][index]
            if window["chunk_id"] not in best or float(score) > best[window["chunk_id"]][0]:
                best[window["chunk_id"]] = (float(score), window)
        results = []
        for chunk_id, (score, window) in sorted(best.items(), key=lambda item: (-item[1][0], item[0])):
            chunk = self.engine.eligible(chunk_id, subject, section, native_only)
            if chunk is None:
                continue
            results.append({"chunk_id": chunk_id, "score": score, "score_kind": "cosine", "matched_window": window, "excerpt": chunk["text"][window["char_start"]:window["char_end"]][:600], "requires_review": chunk["requires_review"], "warnings": chunk["warnings"], "citations": chunk["citations"]})
            if len(results) == limit:
                break
        return results

    def search(self, query, mode="hybrid", limit=5, subject=None, section=None, native_only=False, exact=False):
        if not 1 <= limit <= 100:
            raise SearchFailure("invalid_result_limit")
        if mode not in {"lexical", "semantic", "hybrid"}:
            raise SearchFailure("invalid_search_mode")
        if exact or mode == "lexical":
            return self.engine.lexical(query, limit, subject, section, native_only, exact)
        if mode == "semantic":
            return self.semantic(query, limit, subject, section, native_only)
        depth = max(50, limit)
        lexical = self.engine.lexical(query, depth, subject, section, native_only)
        semantic = self.semantic(query, depth, subject, section, native_only)
        merged = {}
        for channel, rows in [("lexical", lexical), ("semantic", semantic)]:
            for rank, result in enumerate(rows, 1):
                item = merged.setdefault(result["chunk_id"], {**result, "score": 0.0, "score_kind": "rrf", "components": {}})
                item["score"] += 1 / (60 + rank)
                item["components"][channel] = {"rank": rank, "score": result["score"], "score_kind": result["score_kind"]}
        return sorted(merged.values(), key=lambda r: (-r["score"], r["chunk_id"]))[:limit]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ["build", "query"]:
        p = sub.add_parser(name)
        p.add_argument("--database", type=Path, default=DEFAULT_DB)
        p.add_argument("--model-dir", type=Path, default=MODEL_DIR)
        p.add_argument("--index", type=Path, default=INDEX_PATH)
        if name == "query":
            p.add_argument("query")
            p.add_argument("--mode", choices=["lexical", "semantic", "hybrid"], default="hybrid")
            p.add_argument("--limit", type=int, default=5)
            p.add_argument("--subject")
            p.add_argument("--section")
            p.add_argument("--native-only", action="store_true")
            p.add_argument("--exact", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "build":
            build_semantic(args.database, args.model_dir, args.index)
        else:
            retriever = Retriever(args.database, args.model_dir, args.index)
            try:
                results = retriever.search(args.query, args.mode, args.limit, args.subject, args.section, args.native_only, args.exact)
                print(json.dumps({"query": args.query, "mode": args.mode, "answerability": "not_assessed", "results": results}, ensure_ascii=False, indent=2))
            finally:
                retriever.close()
        return 0
    except (SearchFailure, OSError, ValueError, KeyError) as error:
        print(str(error) if isinstance(error, SearchFailure) else "No se ha podido procesar el índice semántico local.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
