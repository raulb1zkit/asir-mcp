#!/usr/bin/env python3
"""Importa el corpus local en Supabase/Postgres y verifica correspondencia exacta.

La importación es transaccional e idempotente: si la verificación falla, la
transacción se revierte y se conserva el estado anterior. No imprime secretos.
"""

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sys

try:
    from .search_material import SearchFailure, sha_file
    from .semantic_search import validate_coverage
    from .supabase_db import DatabaseConfigError, admin_config, connect, vector_literal, use_staging_schema
except ImportError:
    from search_material import SearchFailure, sha_file
    from semantic_search import validate_coverage
    from supabase_db import DatabaseConfigError, admin_config, connect, vector_literal, use_staging_schema

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHUNKS = ROOT / "data/aso/chunks/chunks.jsonl"
DEFAULT_REPORT = ROOT / "data/aso/chunks/report.json"
DEFAULT_SEMANTIC = ROOT / "data/aso/search/semantic.npz"
DEFAULT_MODEL = ROOT / "data/models/multilingual-e5-small"
SCHEMA_VERSION = 1


def load_local(chunks_path, report_path, semantic_path, model_path):
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not report.get("complete") or report.get("output_sha256") != sha_file(chunks_path):
        raise SearchFailure("chunk_report_incomplete_or_stale")
    chunks = []
    for line in chunks_path.read_text(encoding="utf-8").splitlines():
        chunk = json.loads(line)
        digest = hashlib.sha256(chunk["text"].encode()).hexdigest()
        if chunk.get("schema_version") != 1 or chunk.get("text_sha256") != digest or chunk.get("id") != "chunk:" + digest or not chunk.get("citations"):
            raise SearchFailure("invalid_chunk_integrity")
        if chunk.get("characters") != len(chunk["text"]):
            raise SearchFailure("invalid_chunk_characters")
        chunks.append(chunk)
    if not chunks or len({c["id"] for c in chunks}) != len(chunks):
        raise SearchFailure("empty_or_duplicate_chunks")

    manifest = json.loads((model_path / "model.json").read_text(encoding="utf-8"))
    fingerprint = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    try:
        import numpy as np
    except ImportError:
        raise SearchFailure("semantic_dependencies_missing_use_project_venv") from None
    with np.load(semantic_path, allow_pickle=False) as bundle:
        vectors = bundle["vectors"]
        metadata = json.loads(str(bundle["metadata"]))
    if metadata.get("schema_version") != 1:
        raise SearchFailure("unsupported_semantic_index_version")
    if metadata.get("corpus_sha256") != report["output_sha256"]:
        raise SearchFailure("semantic_index_is_stale_or_incompatible")
    if metadata.get("model_fingerprint") != fingerprint or metadata.get("dimensions") != manifest["dimensions"]:
        raise SearchFailure("semantic_model_mismatch")
    windows = metadata.get("windows") or []
    if vectors.shape != (len(windows), manifest["dimensions"]):
        raise SearchFailure("semantic_vectors_invalid")
    if not np.isfinite(vectors).all() or hashlib.sha256(vectors.tobytes()).hexdigest() != metadata.get("vectors_sha256"):
        raise SearchFailure("semantic_vectors_invalid")
    if not np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=5e-4):
        raise SearchFailure("semantic_vectors_not_normalized")
    by_chunk = defaultdict(list)
    for window in windows:
        by_chunk[window["chunk_id"]].append(window)
    chunk_by_id = {c["id"]: c for c in chunks}
    if set(by_chunk) != set(chunk_by_id):
        raise SearchFailure("semantic_index_coverage_incomplete")
    for chunk_id, spans in by_chunk.items():
        validate_coverage(spans, len(chunk_by_id[chunk_id]["text"]), manifest["max_tokens"])
    return {"chunks": chunks, "report": report, "metadata": metadata, "vectors": vectors, "manifest": manifest}


def _extension_schema(cursor, name):
    cursor.execute(
        "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = %s",
        (name,),
    )
    row = cursor.fetchone()
    return row["nspname"] if row else None


def create_schema(connection, *, grant_reader=True):
    from psycopg import sql

    with connection.cursor() as cursor:
        cursor.execute("CREATE SCHEMA IF NOT EXISTS asir")
        cursor.execute("REVOKE ALL ON SCHEMA asir FROM PUBLIC")
        cursor.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions")
        connection.commit()
        try:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA extensions")
            connection.commit()
        except Exception:
            connection.rollback()
        unaccent_schema = _extension_schema(cursor, "unaccent")
        if unaccent_schema:
            body = sql.SQL("SELECT lower({}.unaccent({}::regdictionary, value))").format(
                sql.Identifier(unaccent_schema), sql.Literal(unaccent_schema + ".unaccent")
            )
        else:
            body = sql.SQL("SELECT lower(value)")
        cursor.execute(
            sql.SQL(
                "CREATE OR REPLACE FUNCTION asir.norm_text(value text) RETURNS text "
                "LANGUAGE sql IMMUTABLE PARALLEL SAFE AS {}"
            ).format(sql.Literal(body.as_string(connection)))
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS asir.corpus (
              id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
              schema_version integer NOT NULL,
              corpus_sha256 text NOT NULL CHECK (corpus_sha256 ~ '^[0-9a-f]{64}$'),
              chunker_version text NOT NULL,
              created_at timestamptz NOT NULL,
              report jsonb NOT NULL,
              imported_at timestamptz NOT NULL DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS asir.embedding_metadata (
              id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
              schema_version integer NOT NULL,
              corpus_sha256 text NOT NULL CHECK (corpus_sha256 ~ '^[0-9a-f]{64}$'),
              model_fingerprint text NOT NULL,
              model_repository text NOT NULL,
              model_revision text NOT NULL,
              dimensions integer NOT NULL CHECK (dimensions > 0),
              max_tokens integer NOT NULL CHECK (max_tokens > 0),
              overlap_tokens integer NOT NULL CHECK (overlap_tokens >= 0),
              context text NOT NULL,
              vectors_sha256 text NOT NULL CHECK (vectors_sha256 ~ '^[0-9a-f]{64}$'),
              summary jsonb NOT NULL,
              imported_at timestamptz NOT NULL DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS asir.sources (
              sha256 text PRIMARY KEY CHECK (sha256 ~ '^[0-9a-f]{64}$'),
              subject text NOT NULL,
              section text NOT NULL,
              title text NOT NULL,
              filename text NOT NULL,
              url text,
              mime text,
              path text
            );
            CREATE TABLE IF NOT EXISTS asir.chunks (
              id text PRIMARY KEY CHECK (id ~ '^chunk:[0-9a-f]{64}$'),
              ordinal integer NOT NULL UNIQUE,
              text text NOT NULL,
              text_sha256 text NOT NULL CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),
              characters integer NOT NULL CHECK (characters >= 0),
              warnings jsonb NOT NULL,
              requires_review boolean NOT NULL,
              titles text NOT NULL,
              sections text NOT NULL,
              search_text text GENERATED ALWAYS AS (asir.norm_text(text || E'\\n' || titles || E'\\n' || sections)) STORED,
              search_vector tsvector GENERATED ALWAYS AS (
                setweight(to_tsvector('simple', asir.norm_text(text)), 'A') ||
                setweight(to_tsvector('simple', asir.norm_text(titles)), 'B') ||
                setweight(to_tsvector('simple', asir.norm_text(sections)), 'C')
              ) STORED
            );
            CREATE TABLE IF NOT EXISTS asir.citations (
              id bigserial PRIMARY KEY,
              chunk_id text NOT NULL REFERENCES asir.chunks(id) ON DELETE CASCADE,
              ordinal integer NOT NULL,
              source_sha256 text NOT NULL REFERENCES asir.sources(sha256),
              method text NOT NULL,
              previous_chunk_id text REFERENCES asir.chunks(id) ON DELETE SET NULL,
              next_chunk_id text REFERENCES asir.chunks(id) ON DELETE SET NULL,
              UNIQUE (chunk_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS asir.citation_segments (
              citation_id bigint NOT NULL REFERENCES asir.citations(id) ON DELETE CASCADE,
              ordinal integer NOT NULL,
              source_unit_id text NOT NULL,
              locator jsonb NOT NULL,
              start_char integer NOT NULL CHECK (start_char >= 0),
              end_char integer NOT NULL CHECK (end_char >= start_char),
              method text NOT NULL,
              quality text NOT NULL,
              PRIMARY KEY (citation_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS asir.embedding_windows (
              id bigserial PRIMARY KEY,
              chunk_id text NOT NULL REFERENCES asir.chunks(id) ON DELETE CASCADE,
              ordinal integer NOT NULL,
              char_start integer NOT NULL CHECK (char_start >= 0),
              char_end integer NOT NULL CHECK (char_end > char_start),
              tokens integer NOT NULL CHECK (tokens > 0),
              embedding extensions.vector(384) NOT NULL,
              UNIQUE (chunk_id, ordinal)
            );
            CREATE INDEX IF NOT EXISTS chunks_search_vector_idx ON asir.chunks USING gin (search_vector);
            CREATE INDEX IF NOT EXISTS sources_filter_idx ON asir.sources (subject, section);
            CREATE INDEX IF NOT EXISTS citations_chunk_method_idx ON asir.citations (chunk_id, method);
            CREATE INDEX IF NOT EXISTS citation_segments_citation_idx ON asir.citation_segments (citation_id);
            CREATE INDEX IF NOT EXISTS embedding_windows_chunk_idx ON asir.embedding_windows (chunk_id);

            CREATE TABLE IF NOT EXISTS asir.source_occurrences (
              source_key text PRIMARY KEY CHECK (source_key ~ '^[0-9a-f]{64}$'),
              sha256 text NOT NULL REFERENCES asir.sources(sha256),
              subject text NOT NULL, section text NOT NULL, title text NOT NULL,
              filename text NOT NULL, url text, mime text, path text
            );
            ALTER TABLE asir.citations ADD COLUMN IF NOT EXISTS source_key text REFERENCES asir.source_occurrences(source_key);
            CREATE INDEX IF NOT EXISTS source_occurrences_filter_idx ON asir.source_occurrences(subject, section);
            CREATE OR REPLACE VIEW asir.citation_sources WITH (security_invoker = true) AS
            SELECT ci.id AS citation_id,
              COALESCE(o.sha256, s.sha256) AS sha256,
              COALESCE(o.subject, s.subject) AS subject,
              COALESCE(o.section, s.section) AS section,
              COALESCE(o.title, s.title) AS title,
              COALESCE(o.filename, s.filename) AS filename,
              CASE WHEN o.source_key IS NULL THEN s.url ELSE o.url END AS url,
              CASE WHEN o.source_key IS NULL THEN s.mime ELSE o.mime END AS mime,
              CASE WHEN o.source_key IS NULL THEN s.path ELSE o.path END AS path
            FROM asir.citations ci JOIN asir.sources s ON s.sha256 = ci.source_sha256
            LEFT JOIN asir.source_occurrences o ON o.source_key = ci.source_key;
            """
        )
        if grant_reader:
            cursor.execute("""DO $$ BEGIN
              IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'asir_reader') THEN
                GRANT SELECT ON asir.source_occurrences, asir.citation_sources TO asir_reader;
              END IF;
            END $$""")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS embedding_windows_hnsw_idx "
            "ON asir.embedding_windows USING hnsw (embedding extensions.vector_cosine_ops)"
        )
    connection.commit()


def _titles_and_sections(chunk):
    titles, sections = [], []
    for citation in chunk["citations"]:
        source = citation["source"]
        titles.append(source["title"] + " " + source["filename"])
        sections.append(source["section"])
    return "\n".join(dict.fromkeys(titles)), "\n".join(dict.fromkeys(sections))


def source_key(source):
    fields = {name: source.get(name) for name in ("sha256", "subject", "section", "title", "filename", "url", "mime", "path")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def import_corpus(connection, data):
    chunks = data["chunks"]
    metadata = data["metadata"]
    report = data["report"]
    vectors = data["vectors"]
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL statement_timeout = '180s'")
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext('asir-import'))")
        cursor.execute(
            "TRUNCATE asir.embedding_windows, asir.citation_segments, asir.citations, asir.source_occurrences, "
            "asir.chunks, asir.sources, asir.corpus, asir.embedding_metadata RESTART IDENTITY CASCADE"
        )
        cursor.execute(
            "INSERT INTO asir.corpus (schema_version, corpus_sha256, chunker_version, created_at, report) "
            "VALUES (%s, %s, %s, %s, %s::jsonb)",
            (report["schema_version"], report["output_sha256"], report["chunker_version"], report["created_at"], json.dumps(report)),
        )
        cursor.execute(
            "INSERT INTO asir.embedding_metadata "
            "(schema_version, corpus_sha256, model_fingerprint, model_repository, model_revision, "
            "dimensions, max_tokens, overlap_tokens, context, vectors_sha256, summary) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
            (
                metadata["schema_version"],
                metadata["corpus_sha256"],
                metadata["model_fingerprint"],
                metadata["model_repository"],
                metadata["model_revision"],
                metadata["dimensions"],
                metadata["max_tokens"],
                metadata["overlap_tokens"],
                metadata["context"],
                metadata["vectors_sha256"],
                json.dumps(metadata["summary"]),
            ),
        )

        sources = {}
        occurrences = {}
        for chunk in chunks:
            for citation in chunk["citations"]:
                source = citation["source"]
                sources[source["sha256"]] = source
                occurrences[source_key(source)] = {**source, "source_key": source_key(source)}
        cursor.executemany(
            "INSERT INTO asir.sources (sha256, subject, section, title, filename, url, mime, path) "
            "VALUES (%(sha256)s, %(subject)s, %(section)s, %(title)s, %(filename)s, %(url)s, %(mime)s, %(path)s)",
            list(sources.values()),
        )
        cursor.executemany(
            "INSERT INTO asir.source_occurrences (source_key, sha256, subject, section, title, filename, url, mime, path) "
            "VALUES (%(source_key)s, %(sha256)s, %(subject)s, %(section)s, %(title)s, %(filename)s, %(url)s, %(mime)s, %(path)s)",
            list(occurrences.values()),
        )

        chunk_rows = []
        for ordinal, chunk in enumerate(chunks):
            titles, sections = _titles_and_sections(chunk)
            chunk_rows.append(
                (
                    chunk["id"],
                    ordinal,
                    chunk["text"],
                    chunk["text_sha256"],
                    chunk["characters"],
                    json.dumps(chunk["warnings"]),
                    chunk["requires_review"],
                    titles,
                    sections,
                )
            )
        cursor.executemany(
            "INSERT INTO asir.chunks "
            "(id, ordinal, text, text_sha256, characters, warnings, requires_review, titles, sections) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)",
            chunk_rows,
        )

        citation_rows, segment_rows = [], []
        citation_id = 0
        for chunk in chunks:
            for citation_ordinal, citation in enumerate(chunk["citations"]):
                citation_id += 1
                citation_rows.append((citation_id, chunk["id"], citation_ordinal, citation["source"]["sha256"], source_key(citation["source"]), citation["method"], citation.get("previous_chunk_id"), citation.get("next_chunk_id")))
                segment_rows.extend((citation_id, n, segment["source_unit_id"], json.dumps(segment["locator"]), segment["start"], segment["end"], segment["method"], segment["quality"]) for n, segment in enumerate(citation["segments"]))
        cursor.executemany(
            "INSERT INTO asir.citations (id, chunk_id, ordinal, source_sha256, source_key, method, previous_chunk_id, next_chunk_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            citation_rows,
        )
        cursor.executemany(
            "INSERT INTO asir.citation_segments (citation_id, ordinal, source_unit_id, locator, start_char, end_char, method, quality) VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s,%s)",
            segment_rows,
        )
        if citation_id:
            cursor.execute("SELECT setval(pg_get_serial_sequence('asir.citations', 'id'), %s)", (citation_id,))

        window_rows = []
        by_chunk = defaultdict(list)
        for index, window in enumerate(metadata["windows"]):
            by_chunk[window["chunk_id"]].append((index, window))
        for chunk_id, values in by_chunk.items():
            for ordinal, (vector_index, window) in enumerate(values):
                window_rows.append(
                    (
                        chunk_id,
                        ordinal,
                        window["char_start"],
                        window["char_end"],
                        window["tokens"],
                        vector_literal(vectors[vector_index]),
                    )
                )
        cursor.executemany(
            "INSERT INTO asir.embedding_windows "
            "(chunk_id, ordinal, char_start, char_end, tokens, embedding) "
            "VALUES (%s, %s, %s, %s, %s, %s::extensions.vector)",
            window_rows,
        )
        verify_corpus(connection, data)
    connection.commit()


def _db_chunks(connection):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.id, c.ordinal, c.text, c.text_sha256, c.characters, c.warnings, c.requires_review, "
            "ci.id AS citation_id, ci.ordinal AS citation_ordinal, ci.method, ci.previous_chunk_id, ci.next_chunk_id, "
            "s.sha256 AS source_sha256, s.subject, s.section, s.title, s.filename, s.url, s.mime, s.path, "
            "seg.ordinal AS segment_ordinal, seg.source_unit_id, seg.locator, seg.start_char, seg.end_char, "
            "seg.method AS segment_method, seg.quality "
            "FROM asir.chunks c "
            "JOIN asir.citations ci ON ci.chunk_id = c.id "
            "JOIN asir.citation_sources s ON s.citation_id = ci.id "
            "JOIN asir.citation_segments seg ON seg.citation_id = ci.id "
            "ORDER BY c.ordinal, ci.ordinal, seg.ordinal"
        )
        rows = cursor.fetchall()
    grouped = {}
    for row in rows:
        chunk = grouped.setdefault(
            row["id"],
            {
                "schema_version": 1,
                "id": row["id"],
                "text": row["text"],
                "text_sha256": row["text_sha256"],
                "characters": row["characters"],
                "citations": [],
                "warnings": row["warnings"],
                "requires_review": row["requires_review"],
            },
        )
        citation = next((c for c in chunk["citations"] if c["_id"] == row["citation_id"]), None)
        if citation is None:
            citation = {
                "_id": row["citation_id"],
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
    for chunk in grouped.values():
        for citation in chunk["citations"]:
            citation.pop("_id", None)
    return grouped


def verify_corpus(connection, data):
    import numpy as np

    chunks = data["chunks"]
    metadata = data["metadata"]
    vectors = data["vectors"]
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) AS count FROM asir.sources")
        sources_count = cursor.fetchone()["count"]
        cursor.execute("SELECT count(*) AS count FROM asir.chunks")
        chunks_count = cursor.fetchone()["count"]
        cursor.execute("SELECT count(*) AS count FROM asir.citations")
        citations_count = cursor.fetchone()["count"]
        cursor.execute("SELECT count(*) AS count FROM asir.citation_segments")
        segments_count = cursor.fetchone()["count"]
        cursor.execute("SELECT count(*) AS count FROM asir.embedding_windows")
        windows_count = cursor.fetchone()["count"]
        cursor.execute("SELECT corpus_sha256, schema_version FROM asir.corpus WHERE id = 1")
        corpus = cursor.fetchone()
        cursor.execute(
            "SELECT corpus_sha256, model_fingerprint, dimensions, vectors_sha256 FROM asir.embedding_metadata WHERE id = 1"
        )
        embedding = cursor.fetchone()
        cursor.execute("SELECT count(*) AS count FROM asir.chunks WHERE search_vector IS NULL OR search_text IS NULL")
        null_search = cursor.fetchone()["count"]

    expected_sources = len({cit["source"]["sha256"] for c in chunks for cit in c["citations"]})
    expected_citations = sum(len(c["citations"]) for c in chunks)
    expected_segments = sum(len(cit["segments"]) for c in chunks for cit in c["citations"])
    if (sources_count, chunks_count, citations_count, segments_count, windows_count) != (
        expected_sources,
        len(chunks),
        expected_citations,
        expected_segments,
        len(metadata["windows"]),
    ):
        raise SearchFailure("database_count_mismatch")
    if corpus["corpus_sha256"] != data["report"]["output_sha256"] or corpus["schema_version"] != 1:
        raise SearchFailure("database_corpus_mismatch")
    if (
        embedding["corpus_sha256"] != metadata["corpus_sha256"]
        or embedding["model_fingerprint"] != metadata["model_fingerprint"]
        or embedding["dimensions"] != metadata["dimensions"]
        or embedding["vectors_sha256"] != metadata["vectors_sha256"]
    ):
        raise SearchFailure("database_embedding_metadata_mismatch")
    if null_search:
        raise SearchFailure("database_search_vector_missing")

    db_chunks = _db_chunks(connection)
    if set(db_chunks) != {c["id"] for c in chunks}:
        raise SearchFailure("database_chunk_ids_mismatch")
    for chunk in chunks:
        if db_chunks[chunk["id"]] != chunk:
            raise SearchFailure("database_chunk_payload_mismatch")

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT chunk_id, ordinal, char_start, char_end, tokens, embedding::text AS embedding "
            "FROM asir.embedding_windows ORDER BY chunk_id, ordinal"
        )
        rows = cursor.fetchall()
    by_key = {(row["chunk_id"], row["ordinal"]): row for row in rows}
    fetched = np.empty_like(vectors)
    by_chunk = defaultdict(list)
    for index, window in enumerate(metadata["windows"]):
        by_chunk[window["chunk_id"]].append((index, window))
    for chunk_id, values in by_chunk.items():
        for ordinal, (vector_index, window) in enumerate(values):
            row = by_key.get((chunk_id, ordinal))
            if row is None:
                raise SearchFailure("database_window_missing")
            if (row["char_start"], row["char_end"], row["tokens"]) != (
                window["char_start"],
                window["char_end"],
                window["tokens"],
            ):
                raise SearchFailure("database_window_mismatch")
            parsed = np.asarray(json.loads(row["embedding"]), dtype=np.float32)
            if parsed.shape != (metadata["dimensions"],) or not np.array_equal(parsed, vectors[vector_index]):
                raise SearchFailure("database_vector_mismatch")
            fetched[vector_index] = parsed
    if hashlib.sha256(fetched.tobytes()).hexdigest() != metadata["vectors_sha256"]:
        raise SearchFailure("database_vectors_hash_mismatch")
    return {
        "verified": True,
        "sources": sources_count,
        "chunks": chunks_count,
        "citations": citations_count,
        "segments": segments_count,
        "windows": windows_count,
        "corpus_sha256": corpus["corpus_sha256"],
        "vectors_sha256": embedding["vectors_sha256"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunks", type=Path, default=DEFAULT_CHUNKS)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--semantic", type=Path, default=DEFAULT_SEMANTIC)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--config", type=Path, default=None, help="Configuración administrativa; por defecto .private/supabase-connection.json")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--staging", action="store_true", help="Usa el esquema privado asir_staging; no modifica el corpus servido.")
    args = parser.parse_args()
    try:
        data = load_local(args.chunks, args.report, args.semantic, args.model)
        config = admin_config(args.config) if args.config else admin_config()
        with connect(config, application_name="asir-import", autocommit=False) as connection:
            if args.staging:
                use_staging_schema(connection)
            if not args.verify_only:
                create_schema(connection, grant_reader=not args.staging)
                import_corpus(connection, data)
                status = "imported"
            else:
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL statement_timeout = '60s'")
                report = verify_corpus(connection, data)
                connection.rollback()
                status = "verified"
        if args.verify_only:
            result = report
        else:
            # Reabrir una conexión nueva demuestra que lo importado persistió.
            with connect(config, application_name="asir-verify", autocommit=False) as verification:
                if args.staging:
                    use_staging_schema(verification)
                with verification.cursor() as cursor:
                    cursor.execute("SET LOCAL statement_timeout = '60s'")
                result = verify_corpus(verification, data)
                verification.rollback()
        print(json.dumps({"status": status, **result}, ensure_ascii=False, indent=2))
        return 0
    except (DatabaseConfigError, SearchFailure, OSError, ValueError, KeyError) as error:
        message = str(error) if isinstance(error, (DatabaseConfigError, SearchFailure)) else "import_failed"
        print(json.dumps({"status": "failed", "error": message}, ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"status": "failed", "error": "import_failed"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
