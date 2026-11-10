"""Capa de consulta acotada para MCP; no expone rutas ni credenciales locales."""

import asyncio
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
import json
import os
from pathlib import Path
import re

try:
    from .semantic_search import DEFAULT_DB, INDEX_PATH, MODEL_DIR, Retriever, SearchFailure
except ImportError:
    from semantic_search import DEFAULT_DB, INDEX_PATH, MODEL_DIR, Retriever, SearchFailure

MAX_OUTPUT_BYTES = 48000
# Leave room for omission counters to gain digits after removing an item.
OUTPUT_RESERVE = 64
CHUNK_ID = re.compile(r"chunk:[0-9a-f]{64}\Z")


class MaterialError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    database: Path = DEFAULT_DB
    model_dir: Path = MODEL_DIR
    semantic_index: Path = INDEX_PATH
    allowed_subjects: frozenset[str] = field(default_factory=lambda: frozenset({"ASO"}))
    backend: str = "sqlite"
    max_pending: int = 8
    transport: str = "local_stdio"

    @classmethod
    def from_environment(cls):
        subjects = frozenset(s.strip() for s in os.environ.get("ASIR_ALLOWED_SUBJECTS", "ASO").split(",") if s.strip())
        if not subjects or any(len(s) > 40 for s in subjects):
            raise MaterialError("invalid_server_subjects")
        backend = os.environ.get("ASIR_BACKEND", "sqlite")
        if backend not in {"sqlite", "postgres"}:
            raise MaterialError("invalid_server_backend")
        transport = os.environ.get("ASIR_TRANSPORT", "local_stdio")
        if not re.fullmatch(r"[a-z_]{1,40}", transport):
            raise MaterialError("invalid_server_transport")
        try:
            max_pending = int(os.environ.get("ASIR_MAX_PENDING", "8"))
        except ValueError:
            raise MaterialError("invalid_server_max_pending") from None
        if not 1 <= max_pending <= 64:
            raise MaterialError("invalid_server_max_pending")
        return cls(database=Path(os.environ.get("ASIR_DATABASE", str(DEFAULT_DB))), model_dir=Path(os.environ.get("ASIR_MODEL_DIR", str(MODEL_DIR))), semantic_index=Path(os.environ.get("ASIR_SEMANTIC_INDEX", str(INDEX_PATH))), allowed_subjects=subjects, backend=backend, max_pending=max_pending, transport=transport)


def compact_locations(segments, start=None, end=None):
    values = {"page": set(), "block": set(), "row": set()}
    for segment in segments:
        if start is not None and (segment["end"] <= start or segment["start"] >= end):
            continue
        for key in values:
            if key in segment["locator"]:
                values[key].add(segment["locator"][key])
    spans = []
    for kind, numbers in values.items():
        for value in sorted(numbers):
            if spans and spans[-1]["kind"] == kind and spans[-1]["end"] + 1 == value:
                spans[-1]["end"] = value
            else:
                spans.append({"kind": kind, "start": value, "end": value})
    return spans


def references(citations, start=None, end=None):
    result = []
    for citation in citations:
        locations = compact_locations(citation["segments"], start, end)
        if not locations:
            continue
        source = citation["source"]
        result.append({"source_id": source["sha256"], "subject": source["subject"], "unit": source["section"], "title": source["title"], "filename": source["filename"], "url": source.get("url"), "method": citation["method"], "locations": locations})
    return result


def size_bytes(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


class MaterialService:
    def __init__(self, settings, retriever_factory=None):
        self.settings = settings
        self.factory = retriever_factory or (lambda: Retriever(settings.database, settings.model_dir, settings.semantic_index))
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asir-query")
        self.retriever = None
        self.closed = False
        self.pending = 0
        self.max_pending = settings.max_pending

    async def invoke(self, method, *args):
        if self.closed:
            raise MaterialError("service_closed")
        if self.pending >= self.max_pending:
            raise MaterialError("server_busy")
        self.pending += 1
        try:
            return await asyncio.get_running_loop().run_in_executor(self.worker, partial(method, *args))
        finally:
            self.pending -= 1

    def _open(self):
        if self.retriever is None:
            self.retriever = self.factory()

    async def start(self):
        await self.invoke(self._open)

    async def close(self):
        if self.closed:
            return
        try:
            if self.retriever is not None:
                await self.invoke(self.retriever.close)
        finally:
            self.closed = True
            self.worker.shutdown(wait=True, cancel_futures=True)

    def authorize(self, subject, subjects=None):
        if isinstance(subject, str):
            key = " ".join("".join(c for c in unicodedata.normalize("NFKD", subject.casefold()) if not unicodedata.combining(c)).split())
            aliases = {"bd": "Bases de Datos", "bbdd": "Bases de Datos", "gbd": "Bases de Datos", "bases de datos": "Bases de Datos", "gestion de bases de datos": "Bases de Datos", "xestion de bases de datos": "Bases de Datos", "digitalizacion": "Digitalizacion", "aso": "ASO", "sad": "SAD", "iaw": "IAW"}
            subject = aliases.get(key, subject)
        allowed = self.settings.allowed_subjects if subjects is None else self.settings.allowed_subjects & frozenset(subjects)
        if not allowed or subject not in allowed:
            raise MaterialError("subject_not_available")
        return subject

    async def catalog(self, subjects=None):
        return await self.invoke(self._catalog, subjects)

    def _catalog(self, subjects=None):
        self._open()
        allowed = self.settings.allowed_subjects if subjects is None else self.settings.allowed_subjects & frozenset(subjects)
        engine = self.retriever.engine
        if hasattr(engine, "source_catalog"):
            entries = engine.source_catalog(allowed)
        else:
            catalog = {}
            for chunk in engine.all_chunks():
                for citation in chunk["citations"]:
                    source = citation["source"]
                    if source["subject"] not in allowed:
                        continue
                    item = catalog.setdefault(source["subject"], {"units": set(), "sources": set()})
                    item["units"].add(source["section"])
                    item["sources"].add(source["sha256"])
            entries = [{"name": subject, "units": sorted(item["units"]), "sources": len(item["sources"])} for subject, item in sorted(catalog.items())]
        result = {"corpus_id": engine.metadata["corpus_sha256"], "subjects": entries, "answerability": "not_assessed", "data_is_untrusted": True, "capabilities": ["buscar_material", "leer_fragmento"], "transport": self.settings.transport}
        if size_bytes(result) > MAX_OUTPUT_BYTES:
            raise MaterialError("catalog_response_too_large")
        return result

    async def search(self, query, subject="ASO", unit=None, mode="hybrid", limit=5, native_only=False, exact=False, subjects=None):
        subject = self.authorize(subject, subjects)
        if not isinstance(query, str) or not query.strip() or len(query) > 2000 or not 1 <= limit <= 8 or mode not in {"lexical", "semantic", "hybrid"}:
            raise MaterialError("invalid_search_arguments")
        if unit is not None and (not isinstance(unit, str) or len(unit) > 200):
            raise MaterialError("invalid_unit")
        return await self.invoke(self._search, query, subject, unit, mode, limit, native_only, exact)

    def _search(self, query, subject, unit, mode, limit, native_only, exact):
        self._open()
        rows = self.retriever.search(query, mode, limit, subject, unit, native_only, exact)
        result = {"corpus_id": self.retriever.engine.metadata["corpus_sha256"], "query": query, "mode": "literal" if exact else mode, "answerability": "not_assessed", "data_is_untrusted": True, "results": [], "omitted_results": 0}
        for row in rows:
            refs = references(row["citations"])
            item = {"fragment_id": row["chunk_id"], "score": row["score"], "score_kind": row["score_kind"], "excerpt": row["excerpt"][:700], "requires_review": row["requires_review"], "warnings": row["warnings"], "references": refs[:3], "additional_references": max(0, len(refs) - 3)}
            result["results"].append(item)
            if size_bytes(result) > MAX_OUTPUT_BYTES - OUTPUT_RESERVE:
                result["results"].pop()
                result["omitted_results"] += 1
        if rows and not result["results"]:
            raise MaterialError("search_response_too_large")
        return result

    async def read(self, fragment_id, subject="ASO", unit=None, native_only=False, start=0, characters=8000, neighbors=1, subjects=None):
        subject = self.authorize(subject, subjects)
        if not isinstance(fragment_id, str) or not CHUNK_ID.fullmatch(fragment_id):
            raise MaterialError("invalid_fragment_id")
        if not isinstance(start, int) or start < 0 or not isinstance(characters, int) or not 256 <= characters <= 12000 or neighbors not in {0, 1}:
            raise MaterialError("invalid_read_arguments")
        if unit is not None and (not isinstance(unit, str) or len(unit) > 200):
            raise MaterialError("invalid_unit")
        return await self.invoke(self._read, fragment_id, subject, unit, native_only, start, characters, neighbors)

    def _read(self, fragment_id, subject, unit, native_only, start, characters, neighbors):
        self._open()
        main = self.retriever.engine.eligible(fragment_id, subject, unit, native_only)
        if main is None:
            raise MaterialError("fragment_not_available")
        if start >= len(main["text"]):
            raise MaterialError("fragment_offset_out_of_range")
        end = min(len(main["text"]), start + characters)
        refs = references(main["citations"], start, end)
        result = {"corpus_id": self.retriever.engine.metadata["corpus_sha256"], "answerability": "not_assessed", "data_is_untrusted": True, "fragment": {"fragment_id": fragment_id, "text": main["text"][start:end], "range": {"start": start, "end": end, "total": len(main["text"])}, "next_offset": end if end < len(main["text"]) else None, "requires_review": main["requires_review"], "warnings": main["warnings"], "references": refs[:3], "additional_references": max(0, len(refs) - 3)}, "context": [], "context_omitted": 0}
        if size_bytes(result) > MAX_OUTPUT_BYTES - OUTPUT_RESERVE:
            raise MaterialError("read_response_too_large_reduce_characters")
        if neighbors:
            seen = {fragment_id}
            for citation in main["citations"][:3]:
                for key, relation in [("previous_chunk_id", "previous"), ("next_chunk_id", "next")]:
                    target_id = citation.get(key)
                    if target_id is None or target_id in seen:
                        continue
                    seen.add(target_id)
                    target = self.retriever.engine.eligible(target_id, subject, unit, native_only)
                    if target is None:
                        continue
                    matching = [c for c in target["citations"] if c["source"]["sha256"] == citation["source"]["sha256"] and c["method"] == citation["method"]]
                    if not matching:
                        continue
                    item = {"fragment_id": target_id, "relation": relation, "text": target["text"], "requires_review": target["requires_review"], "warnings": target["warnings"], "references": references(matching)[:3]}
                    result["context"].append(item)
                    if sum(len(c["text"]) for c in result["context"]) + end - start > 16000 or size_bytes(result) > MAX_OUTPUT_BYTES - OUTPUT_RESERVE:
                        result["context"].pop()
                        result["context_omitted"] += 1
        return result
