#!/usr/bin/env python3
"""Measure peak RSS/time while validating and reusing an existing RAG bundle.

Run this in a fresh process on the host that owns the candidate corpus:
`/usr/bin/time -v python tests/benchmark_rag_bundle_reuse.py --manifest ... --bundles ...`.
It performs no network calls and emits no corpus content.
"""

from __future__ import annotations

import argparse
import json
import resource
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.rag_version_backup import bundle_version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--bundles", required=True, type=Path)
    args = parser.parse_args()
    existing_bundle = args.bundles / args.manifest.parent.name
    if existing_bundle.is_symlink() or not existing_bundle.is_dir():
        raise SystemExit("bundle no preexistente; no se modifica el destino")
    started = time.perf_counter()
    result = bundle_version(args.manifest, args.bundles)
    if not result["reused"]:
        raise SystemExit("bundle no preexistente; no se midió la ruta de reutilización")
    usage = resource.getrusage(resource.RUSAGE_SELF)
    print(json.dumps({
        "status": "complete",
        "reused": True,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "peak_rss_kib": usage.ru_maxrss,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
