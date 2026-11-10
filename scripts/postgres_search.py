#!/usr/bin/env python3
"""Consulta híbrida sobre el corpus importado en Supabase/Postgres.

Mantiene el contrato de `semantic_search.Retriever`: el servicio MCP no cambia
su forma de responder, solo el backend que ejecuta búsqueda y lectura.
"""

from contextlib import contextmanager
from pathlib import Path
import unicodedata

try:
    from .search_material import SearchFailure, query_terms
    from .semantic_search import MODEL_DIR, LocalEncoder
    from .supabase_db import connect, vector_literal, use_staging_schema
except ImportError:
    from search_material import SearchFailure, query_terms
    from semantic_search import MODEL_DIR, LocalEncoder
    from supabase_db import connect, vector_literal, use_staging_schema


def _fold_with_map(text):
    characters, positions = [], []
    for index, character in enumerate(text):
        for folded in unicodedata.normalize("NFKD", character.casefold()):
            if not unicodedata.combining(folded):
                characters.append(folded)
                positions.append(index)
    return "".join(characters), positions


def excerpt_for(text, terms=None, literal=None, width=420):
    """Recorta alrededor de la primera coincidencia sin perder signos ni tildes."""
    if literal is not None:
        try:
            start = text.index(literal)
        except ValueError:
            return text[:width]
        start = max(0, start - 120)
        return text[start:start + len(literal) + 300]
    folded, positions = _fold_with_map(text)
    found = []
    for term in terms or []:
        position = folded.find(term)
        if position >= 0:
            found.append((positions[position], positions[min(position + len(term) - 1, len(positions) - 1)] + 1))
    if not found:
        return text[:width]
    first, last = min(found)
    start = max(0, first - 120)
    return text[start:start + max(width, last - start + 180)]


class PostgresEngine:
    def __init__(self, config, model_dir=MODEL_DIR, *, staging=False):
        self.config = config
        self.model_dir = Path(model_dir)
        self.connection = connect(config, application_name="asir-mcp", autocommit=True)
        if staging:
            use_staging_schema(self.connection)
        self.encoder = None
        self.metadata = self._load_metadata()

    @contextmanager
    def _cursor(self):
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("SET LOCAL statement_timeout = '20s'")
                yield cursor

    def _load_metadata(self):
        try:
            with self._cursor() as cursor:
                cursor.execute("SELECT corpus_sha256, schema_version FROM asir.corpus WHERE id = 1")
                corpus = cursor.fetchone()
                cursor.execute(
                    "SELECT model_fingerprint, model_repository, model_revision, dimensions, "
                    "max_tokens, overlap_tokens, context, vectors_sha256, corpus_sha256 "
                    "FROM asir.embedding_metadata WHERE id = 1"
                )
                embedding = cursor.fetchone()
        except Exception:
            raise SearchFailure("database_not_initialized") from None
        if corpus is None or embedding is None:
            raise SearchFailure("database_not_initialized")
        if corpus["schema_version"] != 1 or embedding["corpus_sha256"] != corpus["corpus_sha256"]:
            raise SearchFailure("database_corpus_mismatch")
        return {
            "schema_version": corpus["schema_version"],
            "corpus_sha256": corpus["corpus_sha256"],
            "model_fingerprint": embedding["model_fingerprint"],
            "model_repository": embedding["model_repository"],
            "model_revision": embedding["model_revision"],
            "dimensions": embedding["dimensions"],
            "max_tokens": embedding["max_tokens"],
            "overlap_tokens": embedding["overlap_tokens"],
            "context": embedding["context"],
            "vectors_sha256": embedding["vectors_sha256"],
        }

    def close(self):
        self.connection.close()

    def ping(self):
        with self._cursor() as cursor:
            cursor.execute("SELECT 1 AS ok")
            return cursor.fetchone()["ok"] == 1

    def _exists_filter(self, subject=None, section=None, native_only=False):
        clauses, params = [], []
        if subject is not None:
            clauses.append("s.subject = %s")
            params.append(subject)
        if section is not None:
            clauses.append("s.section = %s")
            params.append(section)
        if native_only:
            clauses.append("ci.method = 'native'")
        condition = " AND " + " AND ".join(clauses) if clauses else ""
        return (
            "EXISTS (SELECT 1 FROM asir.citations ci JOIN asir.citation_sources s ON s.citation_id = ci.id "
            "WHERE ci.chunk_id = c.id" + condition + ")",
            params,
        )

    def _chunk_rows(self, ids=None, subject=None, section=None, native_only=False):
        clauses, params = [], []
        if ids is not None:
            if not ids:
                return {}
            clauses.append("c.id = ANY(%s)")
            params.append(list(ids))
        if subject is not None:
            clauses.append("s.subject = %s")
            params.append(subject)
        if section is not None:
            clauses.append("s.section = %s")
            params.append(section)
        if native_only:
            clauses.append("ci.method = 'native'")
        condition = " AND ".join(clauses) if clauses else "TRUE"
        sql = (
            "SELECT c.id, c.ordinal, c.text, c.text_sha256, c.characters, c.warnings, c.requires_review, "
            "ci.id AS citation_id, ci.ordinal AS citation_ordinal, ci.method, ci.previous_chunk_id, ci.next_chunk_id, "
            "s.sha256 AS source_sha256, s.subject, s.section, s.title, s.filename, s.url, s.mime, s.path, "
            "seg.ordinal AS segment_ordinal, seg.source_unit_id, seg.locator, seg.start_char, seg.end_char, "
            "seg.method AS segment_method, seg.quality "
            "FROM asir.chunks c "
            "JOIN asir.citations ci ON ci.chunk_id = c.id "
            "JOIN asir.citation_sources s ON s.citation_id = ci.id "
            "JOIN asir.citation_segments seg ON seg.citation_id = ci.id "
            "WHERE " + condition + " "
            "ORDER BY c.ordinal, ci.ordinal, seg.ordinal"
        )
        with self._cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        grouped = {}
        citation_index = {}
        for row in rows:
            chunk = grouped.get(row["id"])
            if chunk is None:
                chunk = {
                    "schema_version": 1,
                    "id": row["id"],
                    "text": row["text"],
                    "text_sha256": row["text_sha256"],
                    "characters": row["characters"],
                    "citations": [],
                    "warnings": row["warnings"],
                    "requires_review": row["requires_review"],
                }
                grouped[row["id"]] = chunk
            key = (row["id"], row["citation_id"])
            citation = citation_index.get(key)
            if citation is None:
                citation = {
                    "source": {
                        "sha256": row["source_sha256"],
                        "path": row["path"],
                        "url": row["url"],
                        "title": row["title"],
                        "section": row["section"],
                        "mime": row["mime"],
                        "filename": row["filename"],
                        "subject": row["subject"],
                    },
                    "segments": [],
                    "method": row["method"],
                    "previous_chunk_id": row["previous_chunk_id"],
                    "next_chunk_id": row["next_chunk_id"],
                }
                citation_index[key] = citation
                chunk["citations"].append(citation)
            citation["segments"].append(
                {
                    "source_unit_id": row["source_unit_id"],
                    "locator": row["locator"],
                    "start": row["start_char"],
                    "end": row["end_char"],
                    "method": row["segment_method"],
                    "quality": row["quality"],
                }
            )
        return grouped

    def all_chunks(self):
        return list(self._chunk_rows().values())

    def source_catalog(self, subjects):
        """Agrega metadatos en Postgres sin transferir el texto de todo el corpus."""
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT s.subject, array_agg(DISTINCT s.section ORDER BY s.section) AS units, "
                "count(DISTINCT s.sha256) AS sources FROM asir.citation_sources s "
                "WHERE s.subject = ANY(%s) GROUP BY s.subject ORDER BY s.subject",
                (list(subjects),),
            )
            return [{"name": r["subject"], "units": r["units"], "sources": r["sources"]} for r in cursor.fetchall()]

    def eligible(self, chunk_id, subject=None, section=None, native_only=False):
        return self._chunk_rows([chunk_id], subject, section, native_only).get(chunk_id)

    def lexical(self, query, limit=5, subject=None, section=None, native_only=False, exact=False, *, candidates_only=False):
        if limit < 1 or limit > 100:
            raise SearchFailure("invalid_result_limit")
        terms = query_terms(query)
        condition, filter_params = self._exists_filter(subject, section, native_only)
        if exact:
            sql = (
                "SELECT c.id, c.text FROM asir.chunks c WHERE position(%s in c.text) > 0 AND "
                + condition
                + " ORDER BY length(c.text), c.id LIMIT %s"
            )
            with self._cursor() as cursor:
                cursor.execute(sql, [query, *filter_params, limit])
                rows = cursor.fetchall()
            mode = "literal_case_sensitive"
            results = []
            payloads = self._chunk_rows([row["id"] for row in rows], subject, section, native_only)
            for row in rows:
                chunk = payloads.get(row["id"])
                if chunk is None:
                    continue
                results.append(
                    {
                        "chunk_id": chunk["id"],
                        "score": 1.0,
                        "score_kind": "literal",
                        "lexical_mode": mode,
                        "excerpt": excerpt_for(chunk["text"], literal=query),
                        "requires_review": chunk["requires_review"],
                        "warnings": chunk["warnings"],
                        "citations": chunk["citations"],
                    }
                )
            return results
        if not terms:
            raise SearchFailure("query_has_no_searchable_terms_use_exact")

        def fts(expression):
            sql = (
                "WITH q AS (SELECT to_tsquery('simple', %s) AS query) "
                "SELECT c.id, ts_rank('{0.1,0.2,0.5,1.0}'::float4[], c.search_vector, q.query) AS score "
                "FROM asir.chunks c CROSS JOIN q WHERE c.search_vector @@ q.query AND "
                + condition
                + " ORDER BY score DESC, c.id LIMIT %s"
            )
            with self._cursor() as cursor:
                cursor.execute(sql, [expression, *filter_params, limit])
                return cursor.fetchall()

        rows = fts(" & ".join(terms))
        mode = "all_terms"
        if len(rows) < limit:
            seen = {row["id"] for row in rows}
            added = 0
            for row in fts(" | ".join(terms)):
                if row["id"] in seen:
                    continue
                rows.append(row)
                seen.add(row["id"])
                added += 1
                if len(rows) >= limit:
                    break
            if added:
                mode = "all_terms_then_any_term" if rows and len(rows) > added else "any_term"
        if candidates_only:
            return [{"chunk_id": row["id"], "score": float(row["score"]), "score_kind": "bm25", "lexical_mode": mode} for row in rows]
        payloads = self._chunk_rows([row["id"] for row in rows], subject, section, native_only)
        results = []
        for row in rows:
            chunk = payloads.get(row["id"])
            if chunk is None:
                continue
            results.append(
                {
                    "chunk_id": chunk["id"],
                    "score": float(row["score"]),
                    "score_kind": "bm25",
                    "lexical_mode": mode,
                    "excerpt": excerpt_for(chunk["text"], terms=terms),
                    "requires_review": chunk["requires_review"],
                    "warnings": chunk["warnings"],
                    "citations": chunk["citations"],
                }
            )
        return results

    def load_encoder(self):
        if self.encoder is not None:
            return
        encoder = LocalEncoder(self.model_dir)
        if (
            encoder.fingerprint != self.metadata["model_fingerprint"]
            or encoder.manifest["dimensions"] != self.metadata["dimensions"]
        ):
            raise SearchFailure("semantic_index_is_stale_or_incompatible")
        self.encoder = encoder

    def semantic(self, query, limit=5, subject=None, section=None, native_only=False, *, candidates_only=False):
        if not 1 <= limit <= 100:
            raise SearchFailure("invalid_result_limit")
        self.load_encoder()
        vector = self.encoder.query(query)
        condition, filter_params = self._exists_filter(subject, section, native_only)
        sql = (
            "WITH nearest AS ("
            " SELECT DISTINCT ON (w.chunk_id) w.chunk_id, w.char_start, w.char_end, w.tokens, "
            " w.embedding OPERATOR(extensions.<=>) %s::extensions.vector AS distance "
            " FROM asir.embedding_windows w JOIN asir.chunks c ON c.id = w.chunk_id "
            " WHERE " + condition + " ORDER BY w.chunk_id, distance"
            ") SELECT chunk_id, char_start, char_end, tokens, 1 - distance AS score "
            "FROM nearest ORDER BY distance, chunk_id LIMIT %s"
        )
        literal = vector_literal(vector)
        with self._cursor() as cursor:
            cursor.execute(sql, [literal, *filter_params, limit])
            rows = cursor.fetchall()
        if candidates_only:
            return [{"chunk_id": row["chunk_id"], "score": float(row["score"]), "score_kind": "cosine", "matched_window": {"char_start": row["char_start"], "char_end": row["char_end"], "tokens": row["tokens"]}} for row in rows]
        payloads = self._chunk_rows([row["chunk_id"] for row in rows], subject, section, native_only)
        results = []
        for row in rows:
            chunk = payloads.get(row["chunk_id"])
            if chunk is None:
                continue
            window = {
                "char_start": row["char_start"],
                "char_end": row["char_end"],
                "tokens": row["tokens"],
            }
            results.append(
                {
                    "chunk_id": chunk["id"],
                    "score": float(row["score"]),
                    "score_kind": "cosine",
                    "matched_window": window,
                    "excerpt": chunk["text"][window["char_start"]:window["char_end"]][:600],
                    "requires_review": chunk["requires_review"],
                    "warnings": chunk["warnings"],
                    "citations": chunk["citations"],
                }
            )
        return results


class PostgresRetriever:
    def __init__(self, config, model_dir=MODEL_DIR, *, staging=False):
        self.engine = PostgresEngine(config, model_dir, staging=staging)
        self.model_dir = Path(model_dir)

    @property
    def encoder(self):
        return self.engine.encoder

    def load_semantic(self):
        self.engine.load_encoder()

    def close(self):
        self.engine.close()

    def lexical(self, query, limit=5, subject=None, section=None, native_only=False, exact=False):
        return self.engine.lexical(query, limit, subject, section, native_only, exact)

    def semantic(self, query, limit=5, subject=None, section=None, native_only=False):
        return self.engine.semantic(query, limit, subject, section, native_only)

    def search(self, query, mode="hybrid", limit=5, subject=None, section=None, native_only=False, exact=False):
        if not 1 <= limit <= 100:
            raise SearchFailure("invalid_result_limit")
        if mode not in {"lexical", "semantic", "hybrid"}:
            raise SearchFailure("invalid_search_mode")
        if exact or mode == "lexical":
            return self.engine.lexical(query, limit, subject, section, native_only, exact)
        if mode == "semantic":
            return self.engine.semantic(query, limit, subject, section, native_only)
        depth = max(50, limit)
        lexical = self.engine.lexical(query, depth, subject, section, native_only, candidates_only=True)
        semantic = self.engine.semantic(query, depth, subject, section, native_only, candidates_only=True)
        merged = {}
        for channel, rows in [("lexical", lexical), ("semantic", semantic)]:
            for rank, result in enumerate(rows, 1):
                item = merged.setdefault(
                    result["chunk_id"],
                    {**result, "score": 0.0, "score_kind": "rrf", "components": {}},
                )
                item["score"] += 1 / (60 + rank)
                item["components"][channel] = {
                    "rank": rank,
                    "score": result["score"],
                    "score_kind": result["score_kind"],
                }
        selected = sorted(merged.values(), key=lambda row: (-row["score"], row["chunk_id"]))[:limit]
        payloads = self.engine._chunk_rows([row["chunk_id"] for row in selected], subject, section, native_only)
        results = []
        terms = query_terms(query)
        for row in selected:
            chunk = payloads.get(row["chunk_id"])
            if chunk is None:
                continue
            if "lexical" in row["components"]:
                excerpt = excerpt_for(chunk["text"], terms=terms)
            else:
                window = row["matched_window"]
                excerpt = chunk["text"][window["char_start"]:window["char_end"]][:600]
            results.append({**row, "excerpt": excerpt, "requires_review": chunk["requires_review"], "warnings": chunk["warnings"], "citations": chunk["citations"]})
        return results
