#!/usr/bin/env python3
"""Descarga un grafo ONNX y su tokenizador oficiales; no envía documentos."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "intfloat/multilingual-e5-small"
FILES = ["onnx/model_qint8_avx512_vnni.onnx", "tokenizer.json", "tokenizer_config.json", "config.json"]


def request(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "MCP-ASIR-local-embeddings/0.1"}), timeout=90)


def prepare(output, revision=None, model_file=FILES[0]):
    suffix = "/revision/" + revision if revision else ""
    with request("https://huggingface.co/api/models/" + REPOSITORY + suffix + "?blobs=true") as response:
        metadata = json.load(response)
    revision = metadata["sha"]
    output.mkdir(parents=True, exist_ok=True)
    entries = {item["rfilename"]: item for item in metadata["siblings"]}
    files = []
    selected_files = [model_file, *FILES[1:]]
    for name in selected_files:
        expected = entries[name].get("lfs", {}).get("sha256")
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://huggingface.co/{REPOSITORY}/resolve/{revision}/{name}"
        if not target.exists() or expected is None or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            tmp = target.with_suffix(target.suffix + ".part")
            size = 0
            with request(url) as response, tmp.open("wb") as dest:
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > 600 * 1024 * 1024:
                        raise ValueError("model_file_size_limit")
                    dest.write(block)
            actual = hashlib.sha256(tmp.read_bytes()).hexdigest()
            if expected and expected != actual:
                raise ValueError("model_file_hash_mismatch")
            tmp.replace(target)
        files.append({"path": name, "url": url, "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "bytes": target.stat().st_size})
        print("Modelo local: " + name, file=sys.stderr, flush=True)
    report = {"repository": REPOSITORY, "revision": revision, "model_file": model_file, "tokenizer_file": "tokenizer.json", "max_tokens": 512, "dimensions": 384, "files": files}
    (output / "model.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"repository": REPOSITORY, "revision": revision, "bytes": sum(f["bytes"] for f in files)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data/models/multilingual-e5-small")
    parser.add_argument("--revision")
    parser.add_argument("--model-file", choices=[FILES[0], "onnx/model.onnx"], default=FILES[0])
    args = parser.parse_args()
    prepare(args.output, args.revision, args.model_file)


if __name__ == "__main__":
    main()
