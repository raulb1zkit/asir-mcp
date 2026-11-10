#!/usr/bin/env python3
"""Recalcula una muestra por asignatura del índice vectorial combinado."""

import hashlib
import json
from pathlib import Path

import numpy as np

try:
    from .import_supabase import load_local
    from .moodle_download import write_json
    from .semantic_search import LocalEncoder, MODEL_DIR, ROOT
except ImportError:
    from import_supabase import load_local
    from moodle_download import write_json
    from semantic_search import LocalEncoder, MODEL_DIR, ROOT


def original_batches(windows, indices, batch_size=8):
    order = sorted(range(len(windows)), key=lambda i: windows[i]["tokens"])
    offset_by_index = {index: position for position, index in enumerate(order)}
    starts = sorted({(offset_by_index[index] // batch_size) * batch_size for index in indices})
    return [order[start:start + batch_size] for start in starts]


def main():
    root = ROOT / "data/all"
    data = load_local(root / "chunks/chunks.jsonl", root / "chunks/report.json", root / "search/semantic.npz", MODEL_DIR)
    chunks = {chunk["id"]: chunk for chunk in data["chunks"]}
    by_subject = {}
    for index, window in enumerate(data["metadata"]["windows"]):
        subject = chunks[window["chunk_id"]]["citations"][0]["source"]["subject"]
        by_subject.setdefault(subject, []).append(index)
    encoder = LocalEncoder(MODEL_DIR)
    indices, encodings, selected_counts = [], [], {}
    def encoding_for(index):
        window = data["metadata"]["windows"][index]
        chunk = chunks[window["chunk_id"]]
        source = chunk["citations"][0]["source"]
        prefix = f"passage: {source['subject']} | {source['section'][:120]} | {source['title'][:160]}\n"
        text = chunk["text"][window["char_start"]:window["char_end"]]
        encoded = encoder.tokenizer.encode(prefix + text)
        if len(encoded.ids) != window["tokens"] or len(encoded.ids) > encoder.manifest["max_tokens"]:
            raise ValueError("sample_token_mismatch")
        return encoded

    for subject, positions in sorted(by_subject.items()):
        sample = [positions[int(i)] for i in np.linspace(0, len(positions) - 1, min(8, len(positions)), dtype=int)]
        selected_counts[subject] = len(sample)
        for index in sample:
            indices.append(index)
            encodings.append(encoding_for(index))
    recomputed = encoder.encode(encodings)
    cross_batch = np.sum(recomputed * data["vectors"][indices], axis=1)
    # La cuantización dinámica puede depender del padding. Reconstruir los
    # lotes originales permite verificar por separado la reproducibilidad.
    windows = data["metadata"]["windows"]
    wanted = set(indices)
    reproduced = {}
    for batch in original_batches(windows, indices):
        vectors = encoder.encode([encoding_for(index) for index in batch])
        for position, index in enumerate(batch):
            if index in wanted:
                reproduced[index] = vectors[position]
    similarity = np.asarray([np.dot(reproduced[index], data["vectors"][index]) for index in indices])
    if not np.isfinite(similarity).all() or float(similarity.min()) < 0.9999:
        print(json.dumps({"sample_minimum_cosine": float(similarity.min()), "sample_mean_cosine": float(similarity.mean())}))
        raise ValueError("sample_embedding_mismatch")
    sidecar = json.loads((root / "search/semantic.json").read_text())
    if sidecar != data["metadata"]:
        raise ValueError("semantic_sidecar_mismatch")
    report = {"complete": True, "corpus_sha256": data["report"]["output_sha256"], "windows_verified_for_coverage_and_hash": len(data["metadata"]["windows"]), "reembedded_by_subject": selected_counts, "same_batch_minimum_cosine": float(similarity.min()), "same_batch_mean_cosine": float(similarity.mean()), "same_batch_minimum_accepted_cosine": 0.9999, "different_batch_minimum_cosine": float(cross_batch.min()), "different_batch_mean_cosine": float(cross_batch.mean()), "different_batch_below_0995": int(np.sum(cross_batch < 0.995)), "vectors_sha256": hashlib.sha256(data["vectors"].tobytes()).hexdigest()}
    write_json(root / "audit/vectors.json", report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
