#!/usr/bin/env python3
"""OCR local de páginas sin capa de texto; conserva resultados como derivados."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

try:
    from .extract_text import ExtractionFailure, ROOT, save_json
except ImportError:
    from extract_text import ExtractionFailure, ROOT, save_json

VERSION = "1"


def call(args):
    env = os.environ.copy()
    env["OMP_THREAD_LIMIT"] = "1"
    try:
        return subprocess.run(args, capture_output=True, timeout=120, check=True, env=env).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        raise ExtractionFailure("ocr_tool_failed:" + args[0]) from None


def parse_tsv(raw):
    words, groups = [], {}
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8")), delimiter="\t", quoting=csv.QUOTE_NONE)
    if not reader.fieldnames or not {"level", "conf", "text", "left", "top", "width", "height", "block_num", "par_num", "line_num"} <= set(reader.fieldnames):
        raise ExtractionFailure("ocr_tsv_header_missing")
    for row in reader:
        text = row.get("text", "").strip()
        if not text or row.get("level") != "5":
            continue
        confidence = float(row["conf"])
        if confidence < 0:
            continue
        box = {k: int(row[k]) for k in ["left", "top", "width", "height"]}
        key = tuple(row[k] for k in ["block_num", "par_num", "line_num"])
        groups.setdefault(key, []).append(text)
        words.append({"text": text, "confidence": confidence, **box})
    text = "\n".join(" ".join(line) for line in groups.values())
    mean = sum(w["confidence"] for w in words) / len(words) if words else 0
    return text, words, round(mean, 2)


def model_hashes(directory, languages):
    result = {}
    for language in languages.split("+"):
        if not re.fullmatch(r"[a-z][a-z0-9_]+", language):
            raise ExtractionFailure("invalid_ocr_language")
        path = directory / (language + ".traineddata")
        result[language] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def tasks_from_report(report_path, source_root, scope="missing"):
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not report.get("complete"):
        raise ExtractionFailure("text_extraction_not_complete")
    tasks = []
    seen_sources, seen_units, seen_pages = set(), set(), set()
    for entry in report["documents"]:
        path = (report_path.parent / entry["path"]).resolve()
        if not path.is_relative_to(report_path.parent.resolve()):
            raise ExtractionFailure("derived_path_outside_root")
        document = json.loads(path.read_text(encoding="utf-8"))
        mime = document["source"]["mime"]
        if mime not in {"application/pdf", "application/vnd.oasis.opendocument.text", "image/png", "image/jpeg", "image/tiff", "image/gif"}:
            continue
        needed = [u for u in document["units"] if "needs_ocr" in u["warnings"] or (scope == "images" and (u.get("image_count", 0) > 0 or u.get("kind") == "image"))]
        if not needed:
            continue
        source = document["source"]
        if source["sha256"] in seen_sources:
            raise ExtractionFailure("duplicate_ocr_source")
        seen_sources.add(source["sha256"])
        original = (source_root / source["path"]).resolve()
        if not original.is_relative_to(source_root.resolve()):
            raise ExtractionFailure("original_path_outside_root")
        if hashlib.sha256(original.read_bytes()).hexdigest() != source["sha256"]:
            raise ExtractionFailure("original_hash_mismatch")
        if source["sha256"] != entry["source_sha256"]:
            raise ExtractionFailure("derived_source_mismatch")
        for unit in needed:
            identity = (source["sha256"], unit["id"])
            if identity in seen_units:
                raise ExtractionFailure("duplicate_ocr_unit")
            seen_units.add(identity)
            if unit.get("kind") == "image":
                member = unit["image_member"]
                if not member.startswith("Pictures/") or ".." in Path(member).parts:
                    raise ExtractionFailure("invalid_image_member")
                tasks.append({"source": source, "original": original, "page": None, "image_member": member, "locator": {**unit["locator"], "image": member}, "unit_id": unit["id"]})
                continue
            page = unit["locator"]["page"]
            if not isinstance(page, int) or page < 1 or page > document["extractor"]["page_count"]:
                raise ExtractionFailure("invalid_page_locator")
            page_key = (source["sha256"], page)
            if page_key in seen_pages:
                raise ExtractionFailure("ambiguous_ocr_page")
            seen_pages.add(page_key)
            tasks.append({"source": source, "original": original, "page": page, "unit_id": unit["id"], "standalone_image": mime.startswith("image/")})
    return tasks


def process(task, output, config, tessdata, preview=False):
    key = {"source_sha256": task["source"]["sha256"], "page": task["page"], "config": config}
    if task.get("image_member"):
        key["image_member"] = task["image_member"]
        key["unit_id"] = task["unit_id"]
    fingerprint = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
    suffix = "p" + str(task["page"]) if task["page"] is not None else "img" + hashlib.sha256((task["image_member"] + "|" + task["unit_id"]).encode()).hexdigest()[:16]
    relative = Path("pages") / f"{task['source']['sha256']}-{suffix}.json"
    target = output / relative
    if target.exists():
        cached = json.loads(target.read_text(encoding="utf-8"))
        if cached.get("fingerprint") == fingerprint and cached.get("source_unit_id") == task["unit_id"] and hashlib.sha256(cached["text"].encode()).hexdigest() == cached.get("text_sha256"):
            return {"path": relative.as_posix(), "source_sha256": key["source_sha256"], "page": task["page"], "characters": len(cached["text"]), "mean_confidence": cached["mean_confidence"], "cached": True, "source_unit_id": task["unit_id"]}
    with tempfile.TemporaryDirectory(prefix="mcp-asir-ocr-") as tmp:
        prefix = Path(tmp) / "page"
        if task.get("standalone_image"):
            image_path = task["original"]
        elif task["page"] is not None:
            call(["pdftoppm", "-f", str(task["page"]), "-l", str(task["page"]), "-r", str(config["dpi"]), "-png", "-singlefile", str(task["original"]), str(prefix)])
            image_path = prefix.with_suffix(".png")
        else:
            member = task["image_member"]
            with zipfile.ZipFile(task["original"]) as archive:
                if archive.getinfo(member).file_size > 64 * 1024 * 1024:
                    raise ExtractionFailure("image_member_size_limit")
                image_path = Path(tmp) / ("image" + Path(member).suffix)
                image_path.write_bytes(archive.read(member))
        if preview:
            shutil.copyfile(image_path, output / "sample-page.png")
        raw = call(["tesseract", str(image_path), "stdout", "--tessdata-dir", str(tessdata), "-l", config["languages"], "--psm", str(config["psm"]), "-c", "tessedit_create_tsv=1", "-c", "tessedit_create_txt=0"])
        text, words, confidence = parse_tsv(raw)
    warnings = ["ocr_requires_review", "command_symbols_may_be_wrong", "image_layout_not_preserved"]
    if not text:
        warnings.append("ocr_no_text")
    elif confidence < 80:
        warnings.append("ocr_low_confidence")
    result = {"schema_version": 1, "fingerprint": fingerprint, "source": task["source"], "source_unit_id": task["unit_id"], "locator": task.get("locator", {"page": task["page"]}), "method": "page-raster-ocr" if task["page"] is not None else "embedded-image-ocr", "config": config, "text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "words": words, "mean_confidence": confidence, "warnings": warnings}
    save_json(target, result)
    target.with_suffix(".txt").write_text(text + "\n", encoding="utf-8")
    return {"path": relative.as_posix(), "source_sha256": key["source_sha256"], "page": task["page"], "characters": len(text), "mean_confidence": confidence, "cached": False, "source_unit_id": task["unit_id"]}


def run(report_path, source_root, output, tessdata, languages="spa+eng", sample=False, workers=2, scope="missing"):
    config = {"pipeline_version": VERSION, "tesseract": call(["tesseract", "--version"]).decode().splitlines()[0], "languages": languages, "models_sha256": model_hashes(tessdata, languages), "dpi": 180, "psm": 11, "format": "tsv"}
    tasks = tasks_from_report(report_path, source_root, scope)
    if sample:
        tasks = tasks[:1]
    output.mkdir(parents=True, exist_ok=True)
    report = {"schema_version": 1, "started_at": datetime.now(timezone.utc).isoformat(), "mode": "sample" if sample else "all", "scope": scope, "source_report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(), "config": config, "pages": [], "errors": [], "complete": False}
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending = {pool.submit(process, task, output, config, tessdata, sample): task for task in tasks}
            for future in as_completed(pending):
                task = pending[future]
                try:
                    report["pages"].append(future.result())
                except (ExtractionFailure, OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
                    report["errors"].append({"source_sha256": task["source"]["sha256"], "page": task["page"], "error": str(error) if isinstance(error, ExtractionFailure) else type(error).__name__})
                done = len(report["pages"]) + len(report["errors"])
                if done % 10 == 0 or done == len(tasks):
                    print(f"OCR: {done}/{len(tasks)} unidades procesadas", file=sys.stderr, flush=True)
        report["pages"].sort(key=lambda p: (p["source_sha256"], p["page"] or 0, p["path"]))
        report["complete"] = not report["errors"] and len(report["pages"]) == len(tasks)
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["summary"] = {"selected_pages": len(tasks), "processed_pages": len(report["pages"]), "pdf_pages": sum(p["page"] is not None for p in report["pages"]), "embedded_images": sum(p["page"] is None for p in report["pages"]), "nonempty_pages": sum(bool(p["characters"]) for p in report["pages"]), "characters": sum(p["characters"] for p in report["pages"]), "below_80_confidence": sum(p["mean_confidence"] < 80 for p in report["pages"])}
        save_json(output / "report.json", report)
    print(json.dumps({"complete": report["complete"], **report["summary"], "errors": report["errors"], "report": str(output / "report.json")}, ensure_ascii=False))
    return 0 if report["complete"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["sample", "all"])
    parser.add_argument("--text-report", type=Path, default=ROOT / "data/aso/extracted/all/report.json")
    parser.add_argument("--source-root", type=Path, default=ROOT / "data/aso/moodle")
    parser.add_argument("--tessdata-dir", type=Path, default=ROOT / "data/aso/tools/tessdata")
    parser.add_argument("--languages", default="spa+eng")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--scope", choices=["missing", "images"], default="missing", help="missing: páginas vacías; images: todas las páginas e imágenes ODT reconocidas.")
    args = parser.parse_args()
    if args.workers not in {1, 2, 3, 4}:
        parser.error("--workers debe estar entre 1 y 4")
    try:
        return run(args.text_report, args.source_root, args.output or ROOT / "data/aso/ocr" / args.mode, args.tessdata_dir, args.languages, args.mode == "sample", args.workers, args.scope)
    except (ExtractionFailure, OSError, ValueError, KeyError) as error:
        print(str(error) if isinstance(error, ExtractionFailure) else "No se han podido leer los datos locales de OCR.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
