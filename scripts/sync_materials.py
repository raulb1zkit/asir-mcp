#!/usr/bin/env python3
"""Sincronización nocturna aislada: descarga fresca, construcción y ensayo privado."""
import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import gc
import hashlib
import json
import os
import re
import shutil
import unicodedata
from pathlib import Path
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


class SyncFailure(Exception):
    pass


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    temporary.replace(path)
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def due(state, now, mode="built"):
    """02:00 local; 03:00 de respaldo cubre el salto de primavera."""
    local = now.astimezone(ZoneInfo("Europe/Madrid"))
    return local.hour >= 2 and state.get(mode + "_date", state.get("successful_date") if mode == "built" else None) != local.date().isoformat()


@contextmanager
def exclusive_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as handle:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncFailure("another_sync_is_running") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def checked_report(path):
    report = read_json(path)
    if not report.get("complete") or report.get("issues") or report.get("errors"):
        raise SyncFailure("incomplete_report:" + Path(path).name)
    return report


def _logical_resource_url(url, course_url):
    """Identidad lógica estable de una URL de Moodle; None si no es reconocible.

    Usa los auxiliares existentes del registro de recursos (curso + módulo o
    pluginfile, con los segmentos numéricos de revisión descartados) y exige el
    host del curso. Nunca deriva identidad del hash del contenido. Rutas no
    reconocidas, URLs externas o datos insuficientes devuelven None y mantienen
    el comportamiento previo de comparación por URL exacta.
    """
    if not url or not course_url:
        return None
    try:
        from .rag_resource_registry import RegistryError, _course_identity, _course_url_identity
    except ImportError:
        from rag_resource_registry import RegistryError, _course_identity, _course_url_identity
    try:
        from urllib.parse import urlsplit
        if urlsplit(url).netloc.lower() != urlsplit(course_url).netloc.lower():
            return None
        return _course_url_identity(_course_identity(course_url), url)
    except (RegistryError, ValueError):
        return None


def check_removals(previous, current):
    """Ausencias se conservan; una revisión nueva sustituye al original consultable.

    La sustitución se comprueba por identidad lógica del recurso de Moodle, no
    por URL exacta: una edición cambia el número de revisión del pluginfile y
    la URL antigua no reaparece en el ciclo actual.
    """
    course_url = current.get("course_url") or previous.get("course_url")
    excluded = {x.get("url") for x in current.get("items", []) if x.get("status") == "excluded"}
    current_logical = {}
    for item in current.get("items", []):
        logical = _logical_resource_url(item.get("url"), course_url)
        if logical:
            current_logical.setdefault(logical, set()).add(item.get("sha256"))
    old = {(x["url"], x["sha256"]) for x in previous.get("items", []) if x.get("sha256") and x.get("url") not in excluded}
    new = {(x["url"], x["sha256"]) for x in current.get("items", []) if x.get("sha256")}
    revised_urls = {url for url, sha in new}
    def replaced(url):
        logical = _logical_resource_url(url, course_url)
        return bool(logical and logical in current_logical)
    if {entry for entry in old - new if entry[0] not in revised_urls and not replaced(entry[0])}:
        raise SyncFailure("source_preservation_incomplete")


def _sha_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest_object(root, item):
    root = Path(root)
    relative = Path(item.get("path", ""))
    if root.is_symlink() or not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise SyncFailure("preserved_source_path_invalid")
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise SyncFailure("preserved_source_symlink_rejected")
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise SyncFailure("preserved_source_missing")
    if _sha_file(path) != item.get("sha256"):
        raise SyncFailure("preserved_source_hash_mismatch")
    return path


def preserve_missing_sources(previous_path, current_path, inventory_path):
    """Copia objetos anteriores no publicados ahora y marca su estado Moodle.

    Una edición de recurso de Moodle cambia el número de revisión del
    pluginfile: si la identidad lógica del recurso anterior sigue en el ciclo
    actual (revisión nueva o exclusión expresa), la copia antigua no se arrastra
    como inactiva para siempre. Solo los recursos desaparecidos de verdad se
    conservan inactivos.
    """
    previous_path, current_path = Path(previous_path), Path(current_path)
    previous = checked_report(previous_path)
    current = checked_report(current_path)
    inventory = checked_report(inventory_path)
    course_url = current.get("course_url") or previous.get("course_url")
    excluded = {x.get("url") for x in current.get("items", []) if x.get("status") == "excluded"}
    current_urls = {x.get("url") for x in current.get("items", []) if x.get("status") != "excluded"}
    active_urls = set(current_urls)
    active_urls.update(x.get("resolved_url") for x in inventory.get("items", []) if x.get("resolved_url"))
    current_hashes = {}
    exact = set()
    current_logical = {}
    for item in current.get("items", []):
        logical = _logical_resource_url(item.get("url"), course_url)
        if logical:
            current_logical.setdefault(logical, set()).add(item.get("sha256"))
        if item.get("sha256"):
            current_hashes.setdefault(item.get("url"), set()).add(item["sha256"])
            exact.add((item.get("url"), item["sha256"]))

    preserved = []
    destination_root = current_path.parent
    for item in previous.get("items", []):
        sha, url = item.get("sha256"), item.get("url")
        if not sha or not url or url in excluded or (url, sha) in exact or current_hashes.get(url):
            continue
        logical = _logical_resource_url(url, course_url)
        if logical and logical in current_logical:
            # El mismo recurso lógico existe ya en el ciclo actual con otra
            # revisión (o está excluido a propósito): la revisión antigua no
            # se acumula como inactiva eterna.
            continue
        source = _manifest_object(previous_path.parent, item)
        relative = Path(item["path"])
        if destination_root.is_symlink():
            raise SyncFailure("preserved_destination_symlink_rejected")
        destination = destination_root / relative
        probe = destination_root
        for part in relative.parts:
            probe = probe / part
            if probe.is_symlink():
                raise SyncFailure("preserved_destination_symlink_rejected")
        if not destination.resolve().is_relative_to(destination_root.resolve()):
            raise SyncFailure("preserved_destination_path_invalid")
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_file() or _sha_file(destination) != sha:
                raise SyncFailure("preserved_destination_conflict")
        else:
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source, destination)
        active = url in active_urls
        if current_hashes.get(url) and sha not in current_hashes[url]:
            active = False
        carried = {**item, "status": "downloaded", "moodle_active": active, "preserved_from_previous": True, "change": "preserved"}
        current["items"].append(carried)
        preserved.append(carried)

    check_removals(previous, current)
    current["preservation"] = {
        "previous_sources": sum(bool(item.get("sha256")) for item in previous.get("items", [])),
        "preserved_sources": len(preserved),
        "inactive_preserved_sources": sum(item["moodle_active"] is False for item in preserved),
        "excluded_previous_sources": sum(item.get("url") in excluded and bool(item.get("sha256")) for item in previous.get("items", [])),
    }
    save_json(current_path, current)
    return current["preservation"]


def review_inventory(report):
    for item in report.get("items", []):
        title = "".join(c for c in unicodedata.normalize("NFKD", item.get("title", "").casefold()) if not unicodedata.combining(c))
        if item.get("status") != "excluded" and re.search(r"\b(notas|calificaciones|cualificacions|grades|gradebook)\b", title):
            raise SyncFailure("sensitive_resource_review_required")


def command(script, *args):
    return [sys.executable, str(ROOT / "scripts" / script), *map(str, args)]


def derivation_commands(manifest, destination, subject, tessdata, archives=False, reuse_report=None, previous_root=None):
    prefix = "archives-" if archives else ""
    extracted = destination / (prefix + "extracted/all")
    ocr = destination / (prefix + "ocr/all")
    def reuse(relative):
        path = Path(previous_root) / relative if previous_root else None
        return ["--reuse-report", path] if path and path.is_file() else []
    return [
        command("extract_text.py", "all", "--manifest", manifest, "--output", extracted, "--allow-unsupported", *(["--render-html"] if archives else []), *reuse(prefix + "extracted/all/report.json")),
        command("ocr_pages.py", "all", "--text-report", extracted / "report.json", "--source-root", manifest.parent, "--output", ocr, "--tessdata-dir", tessdata, "--scope", "images", "--workers", 1, *(["--reuse-report", reuse_report] if reuse_report and reuse_report.is_file() else [])),
        command("chunk_text.py", "--text-report", extracted / "report.json", "--ocr-report", ocr / "report.json", "--annotations", destination / "annotations/visual.json", "--output", destination / (prefix + "chunks"), "--subject", subject, *reuse(prefix + "chunks/report.json")),
        command("audit_derived.py", "--root", destination, "--output", destination / (prefix + "audit.json"), *(["--archive"] if archives else [])),
    ]


def ocr_reuse_report(destination, previous_root, slug, resume, archives=False):
    """Reutiliza primero OCR ya validado en el intento que se está reanudando."""
    relative = "archives-ocr/all/report.json" if archives else "ocr/all/report.json"
    candidate = Path(destination) / relative
    if resume and candidate.is_file():
        return candidate
    return Path(previous_root) / slug / relative if previous_root else None


class Runner:
    def __init__(self, directory):
        self.directory = directory
        self.steps = []

    def run(self, argv, timeout=14400):
        # Los argumentos solo contienen rutas y metadatos; ningún secreto.
        index = len(self.steps)
        self.steps.append({"script": Path(argv[1]).name, "complete": False})
        save_json(self.directory / "steps.json", self.steps)
        try:
            result = subprocess.run(argv, cwd=ROOT, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise SyncFailure("step_timeout:" + Path(argv[1]).name) from None
        # No copiar stdout/stderr: podrían contener datos educativos o errores sensibles.
        if result.returncode:
            raise SyncFailure("step_failed:" + Path(argv[1]).name) from None
        self.steps[index]["complete"] = True
        save_json(self.directory / "steps.json", self.steps)


def build(courses, run_root, data_root, session_file, tessdata, runner, credentials_file=None, previous_root=None, resume=False):
    inputs = []
    try:
        from .moodle_auth import ensure_session
    except ImportError:
        from moodle_auth import ensure_session
    for course in courses:
        ensure_session(session_file, credentials_file, course.course_url)
        destination = run_root / course.slug
        manifest = destination / "moodle/manifest.json"
        if not (resume and manifest.is_file()):
            runner.run(command("moodle_download.py", *course.downloader_args(run_root, session_file, mode="inventory")))
            review_inventory(checked_report(destination / "moodle/inventory.json"))
            runner.run(command("moodle_download.py", *course.downloader_args(run_root, session_file)))
        current = checked_report(manifest)
        if resume and (current.get("course_url") != course.course_url or current.get("subject") != course.subject):
            raise SyncFailure("resume_course_mismatch")
        if current.get("course_url") not in (None, course.course_url) or current.get("subject") not in (None, course.subject):
            raise SyncFailure("resume_course_mismatch")
        previous_root = previous_root or data_root
        previous = previous_root / course.slug / "moodle/manifest.json"
        inventory = destination / "moodle/inventory.json"
        if previous.exists():
            preserve_missing_sources(previous, manifest, inventory)
            current = checked_report(manifest)
        else:
            current = checked_report(manifest)
        runner.run(command("audit_import.py", "--manifest", manifest, "--output", destination / "audit-import.json", "--session-file", session_file))
        reuse_report = ocr_reuse_report(destination, previous_root, course.slug, resume)
        for argv in derivation_commands(manifest, destination, course.subject, tessdata, reuse_report=reuse_report, previous_root=previous_root / course.slug):
            runner.run(argv)
        inputs.append(destination / "chunks")
        for source in course.preserve_sources:
            if source.kind == "archives":
                prior_archive = previous_root / course.slug / "archives/manifest.json"
                runner.run(command("expand_archives.py", "--manifest", manifest, "--output", destination / "archives", *(["--reuse-manifest", prior_archive] if prior_archive.is_file() else [])))
                archive_manifest = destination / "archives/manifest.json"
                archive = checked_report(archive_manifest)
                if any(item.get("sha256") for item in archive["items"]):
                    archive_reuse = ocr_reuse_report(destination, previous_root, course.slug, resume, archives=True)
                    for argv in derivation_commands(archive_manifest, destination, course.subject, tessdata, archives=True, reuse_report=archive_reuse, previous_root=previous_root / course.slug):
                        runner.run(argv)
                    inputs.append(destination / "archives-chunks")
            elif source.kind == "manuals":
                # Manuales externos se preservan; no se descargan de Moodle.
                preserved = data_root / source.chunks
                checked_report(preserved / "report.json")
                inputs.append(preserved)
            else:
                raise SyncFailure("unknown_preserved_source_kind")
    merged = run_root / "all/chunks"
    args = [arg for path in inputs for arg in ["--input", path]]
    runner.run(command("merge_corpora.py", *args, "--output", merged))
    checked_report(merged / "report.json")
    return merged


EMBEDDING_CONTEXT = "stable-subject-section-title-v2"
EMBEDDING_OVERLAP_TOKENS = 64


def release_memory():
    """Recolecta y devuelve al sistema el heap liberado tras la inferencia."""
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def embedding_context_for_run(old_index=None, completed_index=None, explicit=None):
    """Conserva el contexto publicado; migrarlo exige una elección explícita."""
    allowed = {'subject-section-title', EMBEDDING_CONTEXT}
    if explicit is not None:
        if explicit not in allowed:
            raise SyncFailure('embedding_context_unsupported')
        return explicit
    import numpy as np
    for path in (old_index, completed_index):
        if path and Path(path).is_file():
            try:
                with np.load(path, allow_pickle=False) as saved:
                    metadata = json.loads(str(saved['metadata'].item()))
                context = metadata['context']
                if context not in allowed or metadata.get('overlap_tokens') != EMBEDDING_OVERLAP_TOKENS:
                    raise ValueError
                return context
            except (OSError, ValueError, KeyError, TypeError):
                raise SyncFailure('previous_embedding_configuration_invalid') from None
    return EMBEDDING_CONTEXT


def reusable_vectors(chunks, destination, model, *, embedding_context=EMBEDDING_CONTEXT):
    """Valida un índice ya escrito sin cargar ONNX; devuelve sus metadatos o None.

    Solo se acepta si el léxico y el vectorial repiten exactamente los chunks
    actuales, con modelo, normalización, cobertura y configuración de contexto
    comprobados. Cualquier desviación obliga a reconstruir desde cero.
    """
    try:
        from .search_material import SearchEngine, SearchFailure
        from .import_supabase import load_local
    except ImportError:
        from search_material import SearchEngine, SearchFailure
        from import_supabase import load_local
    chunks = Path(chunks)
    destination = Path(destination)
    database, path = destination / "lexical.sqlite", destination / "semantic.npz"
    if not database.is_file() or not path.is_file():
        return None
    report = checked_report(chunks / "report.json")
    saved = SearchEngine(database)
    try:
        if saved.metadata.get("corpus_sha256") != report["output_sha256"]:
            return None
        if saved.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            return None
        try:
            data = load_local(chunks / "chunks.jsonl", chunks / "report.json", path, model)
        except (SearchFailure, ValueError, OSError, KeyError):
            return None
        metadata = data["metadata"]
        if metadata.get("context") != embedding_context or metadata.get("overlap_tokens") != EMBEDDING_OVERLAP_TOKENS:
            return None
        # El índice léxico debe contener los mismos fragmentos, en el mismo orden.
        rows = saved.db.execute("SELECT chunk_id, text FROM chunks ORDER BY rowid")
        for chunk in data["chunks"]:
            row = rows.fetchone()
            if row is None or row["chunk_id"] != chunk["id"] or row["text"] != chunk["text"]:
                return None
        if rows.fetchone() is not None:
            return None
        return metadata
    finally:
        saved.close()


def build_vectors(chunks, destination, model, old_database=None, old_index=None, *, embedding_context=None):
    """Reutiliza solo vectores validados y con idéntico texto/contexto E5."""
    try:
        from .search_material import build_index, SearchEngine
        from .semantic_search import LocalEncoder, Retriever, encode_windows
        from .import_supabase import load_local
    except ImportError:
        from search_material import build_index, SearchEngine
        from semantic_search import LocalEncoder, Retriever, encode_windows
        from import_supabase import load_local
    from collections import Counter
    import numpy as np
    database = destination / "lexical.sqlite"
    path = destination / "semantic.npz"
    context = embedding_context_for_run(old_index, path, embedding_context)
    # Un fallo posterior a la escritura atómica no invalida los embeddings.
    # Validarlos sin cargar ONNX permite reanudar sin repetir inferencia.
    completed = reusable_vectors(chunks, destination, model, embedding_context=context)
    if completed:
        save_json(path.with_suffix(".json"), completed)
        print(json.dumps({"step": "embeddings", "completed_index_reused": True}), flush=True)
        return path
    build_index(chunks / "chunks.jsonl", chunks / "report.json", database)
    engine = SearchEngine(database)
    encoder = None
    old = None
    vectors, windows, encodings, fresh_windows = [], [], [], []
    reused = new_count = 0
    try:
        old_chunks, old_indices = {}, {}
        if old_database and old_index:
            old = Retriever(old_database, model, old_index, threads=1)
            old.load_semantic()  # hash, modelo, cobertura y normalización
            encoder = old.encoder
            old_chunks = {c["id"]: c for c in old.engine.all_chunks()}
            for i, window in enumerate(old.metadata["windows"]):
                old_indices.setdefault(window["chunk_id"], []).append(i)
        if encoder is None:
            encoder = LocalEncoder(model, threads=1)
        def prefix(chunk):
            try:
                from .rag_versions import embedding_prefix
            except ImportError:
                from rag_versions import embedding_prefix
            return embedding_prefix(chunk, context)
        def flush_fresh():
            nonlocal new_count
            if not encodings:
                return
            vectors.extend(encoder.encode(encodings))
            windows.extend(fresh_windows)
            new_count += len(fresh_windows)
            print(json.dumps({"step": "embeddings", "new_windows": new_count, "reused_windows": reused}), flush=True)
            encodings.clear()
            fresh_windows.clear()

        for chunk in engine.iter_chunks():
            previous = old_chunks.get(chunk["id"])
            if previous and old.metadata.get("context") == context and old.metadata.get("overlap_tokens") == EMBEDDING_OVERLAP_TOKENS and previous["text"] == chunk["text"] and prefix(previous) == prefix(chunk):
                indices = old_indices[chunk["id"]]
                vectors.extend(old.vectors[indices])
                windows.extend(old.metadata["windows"][i] for i in indices)
                reused += len(indices)
            else:
                for encoding, span in encode_windows(encoder.tokenizer, chunk["text"], prefix(chunk), encoder.manifest["max_tokens"]):
                    encodings.append(encoding)
                    fresh_windows.append({"chunk_id": chunk["id"], **span, "input_sha256": hashlib.sha256((prefix(chunk) + chunk["text"][span["char_start"]:span["char_end"]]).encode()).hexdigest()})
                    if len(encodings) >= 128:
                        flush_fresh()
        flush_fresh()
        array = np.asarray(vectors, dtype=np.float32)
        counts = Counter(w["chunk_id"] for w in windows)
        meta = {"schema_version": 1, "corpus_sha256": engine.metadata["corpus_sha256"], "model_fingerprint": encoder.fingerprint, "model_repository": encoder.manifest["repository"], "model_revision": encoder.manifest["revision"], "dimensions": array.shape[1], "max_tokens": encoder.manifest["max_tokens"], "overlap_tokens": EMBEDDING_OVERLAP_TOKENS, "context": context, "vectors_sha256": hashlib.sha256(array.tobytes()).hexdigest(), "windows": windows, "summary": {"chunks": len(counts), "windows": len(windows), "reused_windows": reused, "new_windows": new_count, "multiwindow_chunks": sum(n > 1 for n in counts.values()), "max_window_tokens": max(w["tokens"] for w in windows)}}
        path = destination / "semantic.npz"
        with path.with_suffix(".tmp").open("wb") as handle:
            np.savez(handle, vectors=array, metadata=np.array(json.dumps(meta)))
        path.with_suffix(".tmp").replace(path)
        save_json(path.with_suffix(".json"), meta)
        # La auditoría verifica hashes, modelo, normalización y cobertura;
        # no necesita una segunda sesión ONNX residente. Liberar inferencia
        # antes de validar evita el pico de memoria que agotaba la cuota.
        del array, meta, vectors, windows, counts
        if old:
            old.encoder, old.vectors, old.metadata = None, None, None
        encoder = None
        release_memory()
        load_local(chunks / "chunks.jsonl", chunks / "report.json", path, model)
        return path
    finally:
        engine.close()
        if old:
            old.close()


def unchanged_corpus(chunks, previous, model):
    """Sin cambios de contenido/citas y con modelo e índice íntegros: no inferir."""
    if not previous:
        return False
    previous = Path(previous)
    current = checked_report(chunks / "report.json")
    old = checked_report(previous / "all/chunks/report.json")
    if current["output_sha256"] != old["output_sha256"]:
        return False
    if _sha_file(chunks / "chunks.jsonl") != current["output_sha256"] or _sha_file(previous / "all/chunks/chunks.jsonl") != old["output_sha256"]:
        raise SyncFailure("unchanged_corpus_hash_mismatch")
    try:
        from .semantic_search import Retriever
    except ImportError:
        from semantic_search import Retriever
    probe = Retriever(previous / "all/search/lexical.sqlite", model, previous / "all/search/semantic.npz")
    try:
        probe.load_semantic()
    finally:
        probe.close()
    return True


def incremental_summary(run_root, courses):
    result = {}
    for course in courses:
        dest = run_root / course.slug
        counts = {}
        for prefix in ("", "archives-"):
            for stage, relative in (("extraction", "extracted/all"), ("ocr", "ocr/all"), ("chunks", "chunks")):
                path = dest / (prefix + relative) / "report.json"
                if path.is_file():
                    summary = checked_report(path).get("summary", {})
                    counts[prefix + stage] = {k: v for k, v in summary.items() if k.startswith(("new_", "reused_"))}
        result[course.slug] = counts
    return result


def validate_staging(chunks, vectors, model, config_path, subjects, verify_only=False):
    try:
        from .import_supabase import load_local, import_corpus, verify_corpus
        from .supabase_db import ingest_config, connect, use_staging_schema
        from .postgres_search import PostgresRetriever
    except ImportError:
        from import_supabase import load_local, import_corpus, verify_corpus
        from supabase_db import ingest_config, connect, use_staging_schema
        from postgres_search import PostgresRetriever
    data = load_local(chunks / "chunks.jsonl", chunks / "report.json", vectors, model)
    config = ingest_config(config_path)
    with connect(config, application_name="asir-nightly-staging", autocommit=False) as connection:
        use_staging_schema(connection)
        if not verify_only:
            import_corpus(connection, data, reset_identity=False)
        verify_corpus(connection, data)  # Antes de COMMIT.
    with connect(config, application_name="asir-nightly-verify", autocommit=False) as connection:
        use_staging_schema(connection)
        verified = verify_corpus(connection, data)
    asyncio.run(smoke_mcp(config, model, subjects, staging=True))
    return verified


async def smoke_mcp(config, model, subjects, staging=False):
    from mcp import Client
    try:
        from .material_service import Settings
        from .mcp_server import create_server
        from .postgres_search import PostgresRetriever
    except ImportError:
        from material_service import Settings
        from mcp_server import create_server
        from postgres_search import PostgresRetriever
    queries = {"ASO": "procesos", "Bases de Datos": "trigger", "IAW": "PHP", "SAD": "seguridad", "Digitalizacion": "nube", "SRI": "DNS", "ACP": "Git Markdown"}
    settings = Settings(backend="postgres", model_dir=model, allowed_subjects=frozenset(subjects), transport="nightly_smoke")
    server = create_server(settings, retriever_factory=lambda: PostgresRetriever(config, model, staging=staging))
    async with Client(server, read_timeout_seconds=120) as client:
        await smoke_client(client, subjects)


async def wait_remote_ready(url, timeout=120):
    """Tras reiniciar, espera a que el endpoint acepte peticiones antes de probarlo."""
    try:
        from .check_remote_mcp import unauthorized_status
    except ImportError:
        from check_remote_mcp import unauthorized_status
    deadline = time.monotonic() + timeout
    delay = 0.5
    while True:
        try:
            if await unauthorized_status(url) == 401:
                return
        except Exception:
            pass
        if time.monotonic() >= deadline:
            raise SyncFailure("remote_mcp_not_ready")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 4)


async def smoke_remote(url, token_file, subjects, corpus_sha256):
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    try:
        from .check_remote_mcp import read_token, unauthorized_status
    except ImportError:
        from check_remote_mcp import read_token, unauthorized_status
    await wait_remote_ready(url)
    if await unauthorized_status(url) != 401 or await unauthorized_status(url, "invalid-token") != 401:
        raise SyncFailure("remote_unauthenticated_access_accepted")
    token = read_token(token_file)
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + token}, timeout=120) as http:
        async with Client(streamable_http_client(url, http_client=http)) as client:
            await smoke_client(client, subjects, corpus_sha256)


async def smoke_client(client, subjects, corpus_sha256=None):
    queries = {"ASO": "procesos", "Bases de Datos": "trigger", "IAW": "PHP", "SAD": "seguridad", "Digitalizacion": "nube", "SRI": "DNS", "ACP": "Git Markdown"}
    catalog = json.loads((await client.read_resource("asir://catalogo")).contents[0].text)
    if {x["name"] for x in catalog["subjects"]} != set(subjects):
        raise SyncFailure("staging_subject_coverage_mismatch")
    for subject in subjects:
        result = await client.call_tool("buscar_material", {"consulta": queries[subject], "asignatura": subject, "limite": 1})
        if result.is_error or not result.structured_content.get("results"):
            raise SyncFailure("mcp_search_failed:" + subject)
        row = result.structured_content["results"][0]
        if not row.get("references") or any(ref["subject"] != subject for ref in row["references"]):
            raise SyncFailure("mcp_subject_scope_failed")
        read = await client.call_tool("leer_fragmento", {"fragmento_id": row["fragment_id"], "asignatura": subject, "vecinos": 0})
        if read.is_error or not read.structured_content.get("fragment", {}).get("text"):
            raise SyncFailure("mcp_read_failed:" + subject)
    rejected = await client.call_tool("buscar_material", {"consulta": "prueba", "asignatura": "FOL"})
    if not rejected.is_error:
        raise SyncFailure("mcp_unauthorized_subject_accepted")
    if corpus_sha256 and catalog.get("corpus_id") != corpus_sha256:
        raise SyncFailure("deployed_mcp_corpus_is_stale")


def publish_candidate_legacy(candidate, previous, model, config_path, subjects, restart, probe):
    """El corpus previo es un artefacto verificado; recuperarlo es otra transacción."""
    try:
        from .import_supabase import load_local, import_corpus, verify_corpus
        from .supabase_db import ingest_config, connect
    except ImportError:
        from import_supabase import load_local, import_corpus, verify_corpus
        from supabase_db import ingest_config, connect
    def load(directory):
        return load_local(directory / "all/chunks/chunks.jsonl", directory / "all/chunks/report.json", directory / "all/search/semantic.npz", model)
    data, backup = load(candidate), load(previous)
    config = ingest_config(config_path)
    # No modificar producción si la copia recuperable no coincide exactamente.
    with connect(config, application_name="asir-nightly-backup-check", autocommit=False) as connection:
        verify_corpus(connection, backup)
    def apply(value):
        with connect(config, application_name="asir-nightly-publish", autocommit=False) as connection:
            import_corpus(connection, value, reset_identity=False)
            verify_corpus(connection, value)
    apply(data)
    try:
        restart()
        probe()
    except Exception:
        try:
            apply(backup)
            restart()
            # Verificación persistida; la prueba del endpoint puede seguir fallando
            # por una caída externa y se reporta por separado.
            with connect(config, application_name="asir-nightly-rollback-check", autocommit=False) as connection:
                verify_corpus(connection, backup)
        except Exception:
            raise SyncFailure("publication_failed_and_rollback_failed") from None
        raise SyncFailure("publication_failed_previous_corpus_restored") from None
    return data["report"]["output_sha256"]



def _registry_map(directory, external_resources_path, diagnostics_path):
    """Mapa de identidades derivado solo del ciclo dado; None sin manifiestos.

    Los manifiestos y chunks usados son exclusivamente los del ciclo propio:
    el candidato y la copia anterior nunca entran en la misma construcción, así
    una revisión conservada inactiva no cuenta como activa simultánea con su
    sustituta. Sin fallback al hash del contenido: lo no identificable falla
    cerrado y deja diagnóstico privado 0600 junto al mapa estable.
    """
    directory = Path(directory) if directory else None
    course_manifests = sorted(directory.glob('*/moodle/manifest.json')) if directory and directory.is_dir() else []
    if not course_manifests:
        # Con el registro de recursos externos ya desplegado, un ciclo sin
        # manifiestos de curso es un candidato roto: falla cerrado. Sin el
        # archivo, las llamadas legacy/sintéticas siguen admitiendo mapa solo.
        if external_resources_path and Path(external_resources_path).is_file():
            raise SyncFailure('rag_course_manifests_missing')
        return None
    try:
        from .rag_resource_registry import Registry, RegistryError
    except ImportError:
        from rag_resource_registry import Registry, RegistryError
    registry = Registry()
    for path in course_manifests:
        checked_report(path)  # valida informe completo; el registro lee la ruta él mismo
        registry.load_course_manifest(path)
    for path in sorted(directory.glob('*/archives/manifest.json')):
        checked_report(path)
        registry.load_archive_manifest(path)
    # El archivo externo es opcional: sin él, un recurso externo citado falla
    # cerrado en la construcción (external_resource_unregistered).
    if external_resources_path and Path(external_resources_path).is_file():
        registry.load_external(external_resources_path)
    try:
        generated, _ = registry.build([directory / 'all/chunks/chunks.jsonl'])
    except RegistryError as error:
        try:
            registry.write_conflicts(diagnostics_path)
        except OSError:
            pass
        raise SyncFailure(str(error)) from error
    return generated


def _merge_resource_maps(static, *derived):
    """Fusión conservando alias estables; identidad en conflicto falla cerrado."""
    merged = dict(static)
    for extra in derived:
        for key, identity in (extra or {}).items():
            known = merged.get(key)
            if known is not None and known != identity:
                raise SyncFailure('rag_resource_identity_conflict')
            merged[key] = identity
    return dict(sorted(merged.items()))


def prepare_delta(candidate, previous, model, resource_map_path, version_root,
                  external_resources_path=None):
    """Validación local y manifiestos privados; sin staging remoto completo."""
    try:
        from .import_supabase import load_local
        from .rag_versions import build_manifest, write_version, plan
    except ImportError:
        from import_supabase import load_local
        from rag_versions import build_manifest, write_version, plan
    if not resource_map_path:
        raise SyncFailure('rag_resource_map_required')
    static_map = read_json(resource_map_path)
    if (not isinstance(static_map, dict)
            or not all(isinstance(key, str) and isinstance(value, str)
                       and value.startswith(('moodle:', 'external:'))
                       for key, value in static_map.items())):
        raise SyncFailure('rag_resource_map_invalid')
    resource_map_path = Path(resource_map_path)
    # Mapas separados por ciclo: el previo y el candidato nunca comparten
    # construcción, así que la revisión conservada de un ciclo anterior no se
    # ve como activa simultánea con la vigente.
    previous_map = _registry_map(previous, external_resources_path, resource_map_path)
    candidate_map = _registry_map(candidate, external_resources_path, resource_map_path)
    resource_map = _merge_resource_maps(static_map, previous_map, candidate_map)
    if previous_map is not None or candidate_map is not None:
        # El refresco determinista se persiste de forma atómica y privada para
        # que los alias estables sobrevivan a ciclos sin cambios.
        save_json(resource_map_path, resource_map)
    def build(directory):
        if not directory:
            raise SyncFailure('rag_reconciled_base_required')
        directory = Path(directory)
        artifacts = {'chunks.jsonl': directory / 'all/chunks/chunks.jsonl',
                     'report.json': directory / 'all/chunks/report.json',
                     'semantic.npz': directory / 'all/search/semantic.npz'}
        value = load_local(artifacts['chunks.jsonl'], artifacts['report.json'], artifacts['semantic.npz'], model)
        manifest = build_manifest(value, resource_map)
        write_version(manifest, version_root, artifacts)
        return manifest
    base, target = build(previous), build(candidate)
    return base, target, plan(base, target)


def publish_candidate(candidate, previous, model, config_path, subjects, restart, probe,
                      *, resource_map_path=None, version_root=None, status_path=None,
                      allow_anomaly=False, legacy_full_publish=False,
                      external_resources_path=None):
    if legacy_full_publish:
        return publish_candidate_legacy(candidate, previous, model, config_path, subjects, restart, probe)
    try:
        from .rag_publish import publish, resolve, write_status
        from .supabase_db import ingest_config, connect
    except ImportError:
        from rag_publish import publish, resolve, write_status
        from supabase_db import ingest_config, connect
    import uuid
    base, target, delta = prepare_delta(candidate, previous, model, resource_map_path, version_root, external_resources_path)
    if delta['blocked'] and not allow_anomaly:
        raise SyncFailure('rag_anomalous_plan')
    status_path = status_path or Path(version_root).parent / 'rag-status.json'
    intent_path = Path(candidate) / 'rag-publication.json'
    if intent_path.exists():
        intent = read_json(intent_path)
        if intent.get('base') != base['version'] or intent.get('target') != target['version']:
            raise SyncFailure('rag_candidate_operation_conflict')
    else:
        intent = {'operation_id': str(uuid.uuid4()), 'rollback_id': str(uuid.uuid4()), 'base': base['version'], 'target': target['version']}
        save_json(intent_path, intent)
    config = ingest_config(config_path)
    factory = lambda: connect(config, application_name='asir-sync-delta', autocommit=True)
    operation_id = intent['operation_id']
    write_status(status_path, phase='prepared', version=target['version'], operation_id=operation_id, counts=delta['counts'])
    # Una recuperación ya confirmada nunca vuelve a publicar automáticamente.
    try:
        from .rag_publish import operation
    except ImportError:
        from rag_publish import operation
    with factory() as conn:
        if operation(conn, intent['rollback_id']):
            raise SyncFailure('rag_previous_attempt_reverted_review_required')
    write_status(status_path, phase='applying', version=target['version'], operation_id=operation_id, counts=delta['counts'])
    try:
        with factory() as conn:
            result = publish(conn, base, target, operation_id, allow_anomaly=allow_anomaly)
    except Exception:
        try:
            result = resolve(factory, operation_id, base, target)
        except Exception:
            write_status(status_path, phase='recovery_required', version=target['version'], operation_id=operation_id)
            raise SyncFailure('rag_commit_resolution_failed') from None
        if result['status'] == 'not_applied':
            write_status(status_path, phase='failed', version=base['version'], operation_id=operation_id)
            raise SyncFailure('rag_not_applied_retry_same_operation') from None
    if result['status'] == 'unchanged':
        write_status(status_path, phase='published', version=base['version'], counts=delta['counts'], published_version=base['version'])
        return base['version']
    resolve(factory, operation_id, base, target)
    write_status(status_path, phase='committed_pending_probe', version=target['version'], operation_id=operation_id, counts=delta['counts'])
    try:
        restart()
        probe()
    except Exception:
        try:
            try:
                with factory() as conn:
                    publish(conn, target, base, intent['rollback_id'], allow_anomaly=True)
            except Exception:
                if resolve(factory, intent['rollback_id'], target, base)['status'] == 'not_applied':
                    raise SyncFailure('rag_inverse_not_applied')
            resolve(factory, intent['rollback_id'], target, base)
            restart()
        except Exception:
            write_status(status_path, phase='recovery_required', version=target['version'], operation_id=operation_id)
            raise SyncFailure('publication_failed_and_rollback_failed') from None
        write_status(status_path, phase='reverted', version=base['version'], operation_id=intent['rollback_id'], published_version=base['version'], previous_version=target['version'])
        raise SyncFailure('publication_failed_previous_corpus_restored') from None
    write_status(status_path, phase='published', version=target['version'], operation_id=operation_id, counts=delta['counts'], published_version=target['version'], previous_version=base['version'])
    return target['version']


def record_sync_failure(status_path, error):
    """Anota el fallo de sincronización en rag-status.json sin borrar lo canónico.

    El monitor vigila la fase compacta: un ciclo que muere antes de publicar
    debe verse en `sync_failed` aunque no se haya tocado la publicación. La
    escritura usa update_status (lock común con el publicador), así que los
    campos canónicos `published_version`, `previous_version` y `pending_versions`
    se conservan tal cual. Fases sin resolver (prepared/applying/committed_
    pending_probe/recovery_required) y sus pines mandan sobre el aviso: solo se
    añade `last_sync_error`, nunca se sobrescribe un commit sin resolver.
    El motivo es seguro por construcción: código interno de SyncFailure o tipo
    de la excepción; nunca rutas, textos de datos ni secretos. Telemetría
    auxiliar: un fallo al anotar no oculta el error original.
    """
    try:
        from .rag_publish import PENDING_PHASES, update_status
    except ImportError:
        from rag_publish import PENDING_PHASES, update_status
    reason = str(error) if isinstance(error, SyncFailure) else type(error).__name__
    def transform(existing):
        phase = existing.get('phase')
        pending = existing.get('pending_versions')
        unresolved = phase in PENDING_PHASES or (isinstance(pending, list) and any(isinstance(value, str) for value in pending))
        if unresolved:
            return {'last_sync_error': reason}
        return {'phase': 'sync_failed', 'sync_error': reason}
    try:
        update_status(status_path, transform)
    except Exception:
        pass


def clear_sync_failure(status_path):
    """Ciclo correcto sin cambios: la versión vigente sigue publicada; limpia el aviso.

    La publicación correcta ya sobrescribe la fase vía write_status; esto cubre
    la salida `unchanged`, que no pasa por el publicador. `sync_error`/`last_
    sync_error` quedan a None (limpio) sin tocar publicado, anterior ni pines.
    """
    try:
        from .rag_publish import update_status
    except ImportError:
        from rag_publish import update_status
    def transform(existing):
        if existing.get('phase') != 'sync_failed':
            return {}
        published = existing.get('published_version') or existing.get('version')
        return {'phase': 'published', 'version': published, 'sync_error': None, 'last_sync_error': None}
    path = Path(status_path)
    if not path.is_file():
        return  # nunca publicado vía delta: no hay aviso que limpiar ni archivo que crear
    try:
        update_status(path, transform)
    except Exception:
        pass


def check_existing_candidate(candidate, courses, data_root, previous_root, runner):
    """Revalida originales y derivados existentes sin red, extracción ni OCR."""
    candidate = candidate.resolve(strict=True)
    if not candidate.is_dir():
        raise SyncFailure("publish_candidate_not_directory")
    for course in courses:
        destination = candidate / course.slug
        report = checked_report(destination / "moodle/manifest.json")
        if report.get("course_url") != course.course_url or report.get("subject") != course.subject:
            raise SyncFailure("publish_candidate_course_mismatch")
        review_inventory(checked_report(destination / "moodle/inventory.json"))
        previous = (previous_root or data_root) / course.slug / "moodle/manifest.json"
        if previous.exists():
            check_removals(checked_report(previous), report)
        runner.run(command("audit_import.py", "--manifest", destination / "moodle/manifest.json", "--output", destination / "acceptance-audit-import.json"))
        runner.run(command("audit_derived.py", "--root", destination, "--output", destination / "acceptance-audit-derived.json"))
        for source in course.preserve_sources:
            if source.kind == "archives":
                archive = checked_report(destination / "archives/manifest.json")
                if any(item.get("sha256") for item in archive["items"]):
                    runner.run(command("audit_derived.py", "--root", destination, "--output", destination / "acceptance-audit-archives.json", "--archive"))
    # Cada entrada conserva los hashes de los chunks fuente que realmente
    # entraron en la fusión, incluidos los manuales preservados.
    merged = checked_report(candidate / "all/chunks/report.json")
    if not merged.get("inputs"):
        raise SyncFailure("publish_candidate_merge_provenance_missing")
    for source in merged["inputs"]:
        directory = Path(source["directory"])
        if hashlib.sha256((directory / "report.json").read_bytes()).hexdigest() != source["report_sha256"] or hashlib.sha256((directory / "chunks.jsonl").read_bytes()).hexdigest() != source["chunks_sha256"]:
            raise SyncFailure("publish_candidate_merge_input_changed")
    return candidate


PREFLIGHT_WARN_PERCENT = 70.0
PREFLIGHT_BLOCK_PERCENT = 85.0
PREFLIGHT_RESERVE_BYTES = 5 * 1024 ** 3
PREFLIGHT_MINIMUM_BYTES = 2 * 1024 ** 3
PREFLIGHT_MARGIN_PERCENT = 50.0


def tree_bytes(root):
    """Suma de tamaños de archivos regulares; nunca sigue enlaces."""
    total = 0
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        return 0
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(current) / d).is_symlink()]
        for name in files:
            candidate = Path(current) / name
            if candidate.is_symlink():
                continue
            try:
                total += candidate.lstat().st_size
            except OSError:
                raise SyncFailure("preflight_inventory_failed") from None
    return total


def estimate_preflight_bytes(base_dirs, *, margin_percent=PREFLIGHT_MARGIN_PERCENT,
                             minimum_bytes=PREFLIGHT_MINIMUM_BYTES):
    """Estimación conservadora desde tamaños medidos, nunca cero inventado.

    base es la medición de los datos que la operación ampliará o sustituirá;
    el margen porcentual cubre temporales (extracción, OCR, índices) y el
    mínimo absoluto evita estimar 0 cuando aún no hay base medible.
    """
    if margin_percent < 0 or minimum_bytes < 0:
        raise SyncFailure("preflight_parameters_invalid")
    measured = sum(tree_bytes(directory) for directory in base_dirs)
    return max(int(measured * (1 + margin_percent / 100.0)), minimum_bytes)


def space_preflight(path, estimate_bytes):
    """Bloquea nueva construcción antes de gastar disco (docs/33: 70/85 %, 5 GiB).

    Usa el ayudante existente de almacenamiento, solo stdlib y sin cargar boto.
    Un bloqueo interrumpe antes de descargar, construir, embeber o publicar:
    el MCP sigue sirviendo la versión publicada. Nunca borra nada para caber.
    """
    try:
        from .rag_storage import SpacePolicy, disk_preflight
    except ImportError:
        from rag_storage import SpacePolicy, disk_preflight
    if not isinstance(estimate_bytes, int) or estimate_bytes <= 0:
        raise SyncFailure("preflight_estimate_required")
    policy = SpacePolicy(warn_percent=PREFLIGHT_WARN_PERCENT, block_percent=PREFLIGHT_BLOCK_PERCENT, reserve_bytes=PREFLIGHT_RESERVE_BYTES)
    result = disk_preflight(path, estimate_bytes, policy)
    if not result["allowed"]:
        raise SyncFailure("disk_preflight_blocked")
    return result


def clear_staging(config_path):
    """Vacía únicamente las ocho tablas de ensayo, conservando esquema y permisos."""
    try:
        from .supabase_db import ingest_config, connect
        from .provision_ingest import TABLES
    except ImportError:
        from supabase_db import ingest_config, connect
        from provision_ingest import TABLES
    from psycopg import sql
    with connect(ingest_config(config_path), application_name="asir-nightly-clear-staging", autocommit=False) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout='60s'")
            cursor.execute("SET LOCAL lock_timeout='10s'")
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext('asir_staging-import'))")
            cursor.execute("SELECT corpus_sha256 FROM asir.corpus WHERE id=1")
            before = cursor.fetchone()["corpus_sha256"]
            names = sql.SQL(',').join(sql.SQL('asir_staging.{}').format(sql.Identifier(table)) for table in TABLES)
            cursor.execute(sql.SQL("TRUNCATE {} CONTINUE IDENTITY RESTRICT").format(names))
            for table in TABLES:
                cursor.execute(sql.SQL("SELECT count(*) AS n FROM asir_staging.{}").format(sql.Identifier(table)))
                if cursor.fetchone()["n"]:
                    raise SyncFailure("staging_cleanup_not_empty")
            cursor.execute("SELECT corpus_sha256 FROM asir.corpus WHERE id=1")
            if cursor.fetchone()["corpus_sha256"] != before:
                raise SyncFailure("served_corpus_changed_during_staging_cleanup")
    return {"complete": True, "tables_emptied": len(TABLES), "served_corpus_unchanged": True}


RUN_NAME = re.compile(r"[0-9]{8}T[0-9]{12}Z\Z")


def prune_runs(state_dir, current, previous=None, keep_other=3):
    """Retiene actual/anterior y tres cachés recientes; nunca sigue enlaces."""
    state_dir = Path(state_dir).resolve(strict=True)
    runs = state_dir / "runs"
    if runs.is_symlink():
        raise SyncFailure("retention_runs_symlink_forbidden")
    if not runs.exists():
        return {"complete": True, "deleted_runs": 0, "retained_other_runs": 0}
    if not runs.is_dir() or not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        raise SyncFailure("retention_safe_directory_operations_unavailable")
    current = Path(current).resolve(strict=True)
    if current.parent != runs or not RUN_NAME.fullmatch(current.name):
        raise SyncFailure("retention_current_candidate_outside_managed_runs")
    protected = {current}
    if previous is not None:
        # Bootstrap puede estar fuera de runs: solo es una referencia protegida,
        # nunca se usa como destino de borrado.
        protected.add(Path(previous).resolve(strict=True))
    if keep_other < 0:
        raise SyncFailure("retention_invalid_limit")
    eligible = []
    skipped = 0
    for entry in runs.iterdir():
        if entry.is_symlink() or not RUN_NAME.fullmatch(entry.name) or not entry.is_dir():
            skipped += 1
            continue
        if entry.resolve(strict=True) not in protected:
            eligible.append(entry)
    eligible.sort(key=lambda path: path.name, reverse=True)
    deleted = 0
    for path in eligible[keep_other:]:
        # Verificar de nuevo antes de borrar. rmtree usa dir_fd y no sigue
        # enlaces simbólicos en plataformas con avoids_symlink_attacks=True.
        if path.parent != runs or path.is_symlink() or path.resolve(strict=True).parent != runs:
            raise SyncFailure("retention_unsafe_run_path")
        shutil.rmtree(path)
        deleted += 1
    return {"complete": True, "deleted_runs": deleted, "retained_other_runs": min(len(eligible), keep_other), "protected_candidates": len(protected), "unmanaged_entries_untouched": skipped}


def finish_publication(state_path, status, cleaner, pruner=None):
    """Guardar el puntero recuperable de forma durable ANTES de vaciar el ensayo."""
    if not status.get("publication") or not status.get("published_candidate") or not status.get("complete"):
        raise SyncFailure("cleanup_requires_successful_durable_publication")
    status["staging_cleanup"] = {"complete": False, "status": "pending"}
    if pruner:
        status["local_retention"] = {"complete": False, "status": "pending"}
    save_json(state_path, status)
    try:
        status["staging_cleanup"] = cleaner()
    except Exception as error:
        status["staging_cleanup"] = {"complete": False, "status": "warning", "error": str(error) if isinstance(error, SyncFailure) else type(error).__name__}
    if pruner:
        try:
            status["local_retention"] = pruner()
        except Exception as error:
            status["local_retention"] = {"complete": False, "status": "warning", "error": str(error) if isinstance(error, SyncFailure) else type(error).__name__}
    # La publicación probada sigue siendo correcta aunque falle la limpieza.
    save_json(state_path, status)
    return status


def validate_resume_run(path, state_dir, now):
    runs = state_dir.resolve() / "runs"
    if runs.is_symlink() or path.is_symlink():
        raise SyncFailure("resume_symlink_forbidden")
    resolved = path.resolve(strict=True)
    if resolved.parent != runs or not resolved.is_dir() or not re.fullmatch(r"\d{8}T\d{12}Z", resolved.name):
        raise SyncFailure("resume_outside_managed_runs")
    created = datetime.strptime(resolved.name, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc)
    if not 0 <= (now-created).total_seconds() <= 86400:
        raise SyncFailure("resume_attempt_too_old")
    return resolved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=ROOT / "config/courses.json")
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument("--state-dir", type=Path, default=ROOT / "data/sync")
    parser.add_argument("--session-file", type=Path, default=ROOT / ".private/moodle-session.json")
    parser.add_argument("--credentials-file", type=Path, default=ROOT / ".private/moodle-credentials.json")
    parser.add_argument("--model", type=Path, default=ROOT / "data/models/multilingual-e5-small")
    parser.add_argument("--tessdata", type=Path, default=ROOT / "data/aso/tools/tessdata")
    parser.add_argument("--database-config", type=Path, default=ROOT / ".private/asir-ingest-connection.json")
    parser.add_argument("--old-database", type=Path)
    parser.add_argument("--old-index", type=Path)
    parser.add_argument("--execute", action="store_true", help="Descarga y construye; por defecto solo muestra el plan.")
    parser.add_argument("--rag-delta-publish", action="store_true", help="Activa recorrido delta solo después de reconciliar y validar la migración.")
    parser.add_argument("--legacy-full-publish", action="store_true", help="Migración inicial explícita: permite recorrido antiguo de recarga completa/staging.")
    parser.add_argument("--rag-resource-map", type=Path, help="Registro privado de identidades lógicas estables.")
    parser.add_argument("--rag-external-resources", type=Path, default=Path("/var/lib/asir-sync/external-resources.json"), help="Registro explícito privado {URL: external:id} para recursos externos.")
    parser.add_argument("--embedding-context", choices=("subject-section-title", EMBEDDING_CONTEXT), help="Migración explícita del prefijo; por defecto conserva el contexto publicado.")
    parser.add_argument("--rag-allow-anomaly", action="store_true", help="Autoriza expresamente plan masivo/cambio de modelo.")
    parser.add_argument("--preflight-margin-percent", type=float, default=PREFLIGHT_MARGIN_PERCENT, help="Margen porcentual sobre la base medida para la estimación de espacio.")
    parser.add_argument("--preflight-minimum-bytes", type=int, default=PREFLIGHT_MINIMUM_BYTES, help="Estimación mínima absoluta cuando no hay base medible; nunca se inventa cero.")
    parser.add_argument("--staging", action="store_true", help="Carga y verifica el ensayo privado tras construir.")
    parser.add_argument("--publish", action="store_true", help="Publica solo tras ensayo; requiere --rollback-candidate y reinicio del MCP.")
    parser.add_argument("--resume-run", type=Path, help="Reanuda un intento de menos de 24h; vuelve a auditar y aprovecha el OCR ya válido.")
    parser.add_argument("--publish-candidate", type=Path, help="Publica un candidato ya construido y cargado en ensayo, sin repetir descarga ni OCR.")
    parser.add_argument("--rollback-candidate", type=Path, help="Directorio de ejecución cuyo corpus coincide con producción.")
    parser.add_argument("--probe-token-file", type=Path)
    parser.add_argument("--probe-url", default="http://127.0.0.1:8080/mcp")
    parser.add_argument("--scheduled", action="store_true", help="Respeta una ejecución correcta por fecha de Madrid.")
    args = parser.parse_args()
    sync_failure_recorded = False
    try:
        try:
            from .course_registry import load_registry
        except ImportError:
            from course_registry import load_registry
        courses = load_registry(args.registry)
        if args.rag_delta_publish and args.legacy_full_publish:
            raise SyncFailure("rag_publication_modes_are_exclusive")
        if args.execute and (args.publish or args.staging) and not (args.rag_delta_publish or args.legacy_full_publish):
            raise SyncFailure("rag_migration_activation_required")
        if args.publish_candidate and (not args.publish or (args.legacy_full_publish and not args.staging)):
            raise SyncFailure("publish_candidate_requires_publish_and_staging")
        if args.publish and args.legacy_full_publish and not args.staging:
            raise SyncFailure("publish_requires_staging")
        if not args.execute:
            print(json.dumps({"mode": "plan", "subjects": [c.subject for c in courses], "publication": args.publish, "staging": args.staging}, ensure_ascii=False))
            return 0
        with exclusive_lock(args.state_dir / "sync.lock"):
            state_path = args.state_dir / "status.json"
            state = read_json(state_path) if state_path.exists() else {}
            rollback_candidate = Path(state["published_candidate"]) if state.get("published_candidate") else args.rollback_candidate
            now = datetime.now(timezone.utc)
            mode = "published" if args.publish else ("staged" if args.staging else "built")
            if args.publish and (not rollback_candidate or not args.probe_token_file):
                raise SyncFailure("publish_requires_rollback_candidate_and_probe_token_file")
            if args.scheduled and not due(state, now, mode):
                return 0
            if args.resume_run and args.publish_candidate:
                raise SyncFailure("resume_and_publish_candidate_are_exclusive")
            run_root = validate_resume_run(args.resume_run, args.state_dir, now) if args.resume_run else (args.publish_candidate.resolve(strict=True) if args.publish_candidate else args.state_dir / "runs" / now.strftime("%Y%m%dT%H%M%S%fZ"))
            status = {"complete": False, "started_at": now.isoformat(), "step": "authentication", "publication": False}
            for key in ("successful_date", "built_date", "staged_date", "published_date", "published_candidate", "previous_candidate", "candidate", "corpus_sha256", "rag_version", "rag_manifest_path"):
                if key in state:
                    status[key] = state[key]
            save_json(state_path, status)
            try:
                # Preflight de espacio antes de crear la ejecución, descargar o
                # construir: si bloquea, no hay red, embeddings ni publicación
                # y el MCP conserva el corpus servido.
                prior = state.get("published_candidate") or state.get("candidate")
                preflight_bases = [args.data_root]
                if prior:
                    preflight_bases.append(Path(prior))
                estimate = estimate_preflight_bytes(preflight_bases, margin_percent=args.preflight_margin_percent, minimum_bytes=args.preflight_minimum_bytes)
                status["space"] = space_preflight(args.state_dir, estimate)
                save_json(state_path, status)
                if args.publish_candidate:
                    runner = Runner(run_root / "acceptance" / now.strftime("%Y%m%dT%H%M%S%fZ"))
                    run_root = check_existing_candidate(run_root, courses, args.data_root, Path(prior) if prior else None, runner)
                    chunks = run_root / "all/chunks"
                    vectors = run_root / "all/search/semantic.npz"
                else:
                    runner = Runner(run_root)
                    runner.run(command("moodle_auth.py", "check", "--session-file", args.session_file, "--credentials-file", args.credentials_file))
                    status["step"] = "building"
                    save_json(state_path, status)
                    prior = state.get("published_candidate") or state.get("candidate")
                    chunks = build(courses, run_root, args.data_root, args.session_file, args.tessdata, runner, args.credentials_file, Path(prior) if prior else None, resume=bool(args.resume_run))
                    status["incremental"] = incremental_summary(run_root, courses)
                    save_json(state_path, status)
                    # Solo omitir staging/publicación cuando coincide con el corpus
                    # ya publicado, nunca por coincidir con un ensayo no publicado.
                    if state.get("published_candidate") and unchanged_corpus(chunks, state["published_candidate"], args.model):
                        if args.publish:
                            asyncio.run(smoke_remote(args.probe_url, args.probe_token_file, [c.subject for c in courses], (checked_report(chunks / "report.json")["output_sha256"] if args.legacy_full_publish else state.get("corpus_sha256"))))
                        status.update(complete=True, step="unchanged", candidate=state["published_candidate"], checked_candidate=str(run_root), finished_at=datetime.now(timezone.utc).isoformat(), new_windows=0)
                        status[mode + "_date"] = now.astimezone(ZoneInfo("Europe/Madrid")).date().isoformat()
                        status["successful_date"] = status[mode + "_date"]
                        published = Path(state["published_candidate"])
                        if args.legacy_full_publish and published.resolve().parent == (args.state_dir / "runs").resolve():
                            status["local_retention"] = prune_runs(args.state_dir, published, state.get("previous_candidate"))
                        save_json(state_path, status)
                        # Ciclo correcto sin cambios: la versión publicada sigue
                        # vigente; se limpia cualquier aviso de fallo previo.
                        clear_sync_failure(args.state_dir / "rag-status.json")
                        print(json.dumps({"complete": True, "step": "unchanged", "publication": False, "new_windows": 0}))
                        return 0
                    old_database, old_index = args.old_database, args.old_index
                    prior_candidate = state.get("published_candidate") or state.get("candidate")
                    if not old_database and not old_index and prior_candidate:
                        old_database = Path(prior_candidate) / "all/search/lexical.sqlite"
                        old_index = Path(prior_candidate) / "all/search/semantic.npz"
                    # Segundo preflight antes de inferir: el índice nuevo puede
                    # duplicar temporalmente el tamaño del anterior.
                    embed_bases = [run_root]
                    if prior_candidate:
                        embed_bases.append(Path(prior_candidate) / "all/search")
                    embed_estimate = estimate_preflight_bytes(embed_bases, margin_percent=args.preflight_margin_percent, minimum_bytes=args.preflight_minimum_bytes)
                    status["space"] = space_preflight(args.state_dir, embed_estimate)
                    status["step"] = "embeddings"
                    save_json(state_path, status)
                    vectors = build_vectors(chunks, run_root / "all/search", args.model, old_database, old_index, embedding_context=args.embedding_context)
                    status["vectors"] = read_json(vectors.with_suffix(".json"))["summary"]
                    release_memory()
                if args.staging and args.legacy_full_publish:
                    status["step"] = "staging"
                    save_json(state_path, status)
                    status["verification"] = validate_staging(chunks, vectors, args.model, args.database_config, [c.subject for c in courses], verify_only=bool(args.publish_candidate))
                if (args.publish or args.staging) and not args.legacy_full_publish:
                    _, _, delta = prepare_delta(run_root, rollback_candidate, args.model, args.rag_resource_map, args.state_dir / "versioning", args.rag_external_resources)
                    status['verification'] = {k: v for k, v in delta.items() if k != 'actions'}
                    if delta['blocked'] and not args.rag_allow_anomaly:
                        raise SyncFailure('rag_anomalous_plan')
                if args.publish:
                    try:
                        from .supabase_db import ingest_config
                    except ImportError:
                        from supabase_db import ingest_config
                    preflight = subprocess.run(["sudo", "-n", "-l", "/usr/bin/systemctl", "restart", "asir-mcp.service"], capture_output=True, timeout=30)
                    if preflight.returncode:
                        raise SyncFailure("mcp_restart_privilege_missing")
                    def restart():
                        result = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "restart", "asir-mcp.service"], capture_output=True, timeout=60)
                        if result.returncode:
                            raise SyncFailure("mcp_restart_failed")
                    def probe():
                        asyncio.run(smoke_remote(args.probe_url, args.probe_token_file, [c.subject for c in courses], (checked_report(chunks / "report.json")["output_sha256"] if args.legacy_full_publish else status["verification"]["target"])))
                    status["corpus_sha256"] = publish_candidate(run_root, rollback_candidate, args.model, args.database_config, [c.subject for c in courses], restart, probe, resource_map_path=args.rag_resource_map, version_root=args.state_dir / "versioning", status_path=args.state_dir / "rag-status.json", allow_anomaly=args.rag_allow_anomaly, legacy_full_publish=args.legacy_full_publish, external_resources_path=args.rag_external_resources)
                    if not args.legacy_full_publish:
                        status["rag_version"] = status["corpus_sha256"]
                        status["rag_manifest_path"] = str((args.state_dir / "versioning" / status["rag_version"] / "manifest.json").resolve())
                    status["publication"] = True
                    status["published_candidate"] = str(run_root)
                    status["previous_candidate"] = str(rollback_candidate)
                status[mode + "_date"] = now.astimezone(ZoneInfo("Europe/Madrid")).date().isoformat()
                status.update(complete=True, step="published" if args.publish else ("staged" if args.staging else "built"), successful_date=now.astimezone(ZoneInfo("Europe/Madrid")).date().isoformat(), candidate=str(run_root), finished_at=datetime.now(timezone.utc).isoformat())
                if args.publish:
                    finish_publication(state_path, status, lambda: clear_staging(args.database_config) if args.legacy_full_publish else {"complete": True, "mode": "delta_no_remote_staging"}, lambda: prune_runs(args.state_dir, run_root, rollback_candidate) if args.legacy_full_publish else {"status": "pending_version_retention_integration"})
                else:
                    save_json(state_path, status)
                print(json.dumps(status, ensure_ascii=False))
                return 0
            except Exception as error:
                status.update(step="failed", error=str(error) if isinstance(error, SyncFailure) else type(error).__name__, finished_at=datetime.now(timezone.utc).isoformat())
                save_json(state_path, status)
                # Aviso compacto para el monitor antes de propagar el fallo;
                # nunca sobrescribe publicado/anterior/pines sin resolver.
                record_sync_failure(args.state_dir / "rag-status.json", error)
                sync_failure_recorded = True
                raise
    except Exception as error:
        if not sync_failure_recorded:
            record_sync_failure(args.state_dir / "rag-status.json", error)
        print(json.dumps({"complete": False, "error": str(error) if isinstance(error, SyncFailure) else type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
