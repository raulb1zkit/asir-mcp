#!/usr/bin/env python3
"""Verifica hashes, tamaños y firmas de originales sin imprimir contenido."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import zipfile

try:
    from .moodle_download import load_cookie, write_json
except ImportError:
    from moodle_download import load_cookie, write_json


def audit(manifests, output, session_file=None):
    secret = load_cookie(session_file).partition("=")[2].encode() if session_file else None
    summary, issues = [], []
    for manifest_path in manifests:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not manifest.get("complete") or manifest.get("issues"):
            raise ValueError("manifest_incomplete")
        seen, types = set(), Counter()
        total = 0
        for item in manifest["items"]:
            sha = item.get("sha256")
            if not sha or sha in seen:
                continue
            seen.add(sha)
            path = (manifest_path.parent / item["path"]).resolve()
            if not path.is_relative_to(manifest_path.parent.resolve()):
                issues.append({"sha256": sha, "error": "path_outside_root"})
                continue
            content = path.read_bytes()
            total += len(content)
            types[item["mime"]] += 1
            if hashlib.sha256(content).hexdigest() != sha or len(content) != item["bytes"]:
                issues.append({"sha256": sha, "error": "hash_or_size_mismatch"})
            if secret and secret in content:
                issues.append({"sha256": sha, "error": "session_value_in_object"})
            mime = item["mime"]
            if mime == "application/pdf" and not content.startswith(b"%PDF-"):
                issues.append({"sha256": sha, "error": "pdf_signature_mismatch"})
            if mime in {"application/zip", "application/vnd.oasis.opendocument.text", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}:
                try:
                    with zipfile.ZipFile(path) as archive:
                        if sum(i.file_size for i in archive.infolist()) > 512 * 1024 * 1024:
                            issues.append({"sha256": sha, "error": "zip_uncompressed_size_limit"})
                        elif archive.testzip() is not None:
                            issues.append({"sha256": sha, "error": "zip_integrity_failed"})
                except (OSError, zipfile.BadZipFile):
                    issues.append({"sha256": sha, "error": "zip_invalid"})
        summary.append({"manifest": str(manifest_path), "objects": len(seen), "bytes": total, "mime_counts": dict(types)})
    report = {"complete": not issues, "session_value_checked": secret is not None, "manifests": summary, "issues": issues}
    write_json(output, report)
    print(json.dumps({"complete": report["complete"], "objects": sum(i["objects"] for i in summary), "issues": len(issues), "report": str(output)}))
    return 0 if not issues else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--session-file", type=Path)
    args = parser.parse_args()
    return audit(args.manifest, args.output, args.session_file)


if __name__ == "__main__":
    raise SystemExit(main())
