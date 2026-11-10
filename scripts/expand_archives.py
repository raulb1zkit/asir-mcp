#!/usr/bin/env python3
"""Expande texto educativo de archivos comprimidos con límites y procedencia.

No ejecuta archivos ni extrae rutas del archivo directamente al sistema.
El manifiesto resultante es una entrada independiente para extract_text.py.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import tempfile
import zipfile

try:
    from .moodle_download import store, write_json
except ImportError:
    from moodle_download import store, write_json

MAX_MEMBERS = 16000
MAX_MEMBER = 4 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
MIMES = {".html": "text/html", ".htm": "text/html", ".php": "text/plain", ".md": "text/plain", ".txt": "text/plain", ".sql": "text/plain", ".yml": "text/plain", ".yaml": "text/plain", ".ini": "text/plain", ".css": "text/css", ".js": "text/plain"}
PRIVATE_NAMES = {".env", "id_rsa", "id_ed25519", "credentials", "credentials.json"}


def member_mime(name):
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(p in {"..", ".git", ".private"} for p in path.parts):
        return None
    if any(p.lower() in PRIVATE_NAMES or p.lower().startswith(".env.") for p in path.parts):
        return None
    return MIMES.get(path.suffix.lower()) or ("text/plain" if path.name.lower() == "dockerfile" else None)


def archive_members(path, mime):
    if mime == "application/g-zip":
        with tarfile.open(path, "r:gz") as archive:
            for item in archive:
                if item.isfile() and member_mime(item.name):
                    if item.size > MAX_MEMBER:
                        raise ValueError("archive_member_size_limit")
                    stream = archive.extractfile(item)
                    if stream is None:
                        raise ValueError("archive_member_missing")
                    yield item.name, stream.read(MAX_MEMBER + 1)
    elif mime == "application/zip":
        with zipfile.ZipFile(path) as archive:
            for item in archive.infolist():
                if not item.is_dir() and member_mime(item.filename):
                    if item.file_size > MAX_MEMBER:
                        raise ValueError("archive_member_size_limit")
                    yield item.filename, archive.read(item)
    elif mime == "application/x-7z-compressed":
        listing = subprocess.run(["bsdtar", "-tf", str(path)], check=True, capture_output=True, timeout=60).stdout.decode("utf-8")
        names = listing.splitlines()
        if len(names) > MAX_MEMBERS:
            raise ValueError("archive_member_limit")
        for name in names:
            if member_mime(name):
                if any(character in name for character in "*?[]"):
                    raise ValueError("archive_member_pattern_unsupported")
                # `--` impide que el nombre se interprete como opción. El
                # límite del archivo de salida acota la expansión de cada miembro.
                import resource
                with tempfile.TemporaryFile() as stream:
                    def limit_output():
                        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_MEMBER, MAX_MEMBER))
                    subprocess.run(["bsdtar", "-xOf", str(path), "--", name], check=True, stdout=stream, stderr=subprocess.DEVNULL, timeout=60, preexec_fn=limit_output)
                    stream.seek(0)
                    yield name, stream.read(MAX_MEMBER + 1)
    else:
        raise ValueError("archive_format_unsupported")


def run(manifest_path, output):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not manifest.get("complete") or manifest.get("issues"):
        raise ValueError("incomplete_manifest")
    result = {"version": 1, "course_url": manifest["course_url"], "subject": manifest["subject"], "complete": False, "items": [], "issues": [], "archives": []}
    count = total = 0
    for source in manifest["items"]:
        if source.get("mime") not in {"application/g-zip", "application/zip", "application/x-7z-compressed"} or not source.get("sha256"):
            continue
        path = (manifest_path.parent / source["path"]).resolve()
        if not path.is_relative_to(manifest_path.parent.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"]:
            raise ValueError("archive_integrity_failed")
        entry = {"sha256": source["sha256"], "filename": source["filename"], "members": 0}
        result["archives"].append(entry)
        for name, content in archive_members(path, source["mime"]):
            count += 1
            total += len(content)
            if count > MAX_MEMBERS or len(content) > MAX_MEMBER or total > MAX_TOTAL:
                raise ValueError("archive_expansion_limit")
            saved = store(output, content)
            result["items"].append({**saved, "status": "downloaded", "kind": "archive-member", "url": source["url"], "title": source["title"] + " / " + name, "section": source["section"], "filename": name, "mime": member_mime(name), "archive_sha256": source["sha256"], "archive_member": name})
            entry["members"] += 1
    result["complete"] = True
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    result["expanded_bytes"] = total
    write_json(output / "manifest.json", result)
    print(json.dumps({"members": count, "bytes": total, "archives": result["archives"]}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.manifest, args.output)


if __name__ == "__main__":
    main()
