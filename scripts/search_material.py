#!/usr/bin/env python3
"""Índice local y consulta léxica con SQLite FTS5, sin APIs de IA."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data/aso/search/lexical.sqlite"
STOPWORDS = set("a al algo algun alguna algunos algunas ante antes aqui asi aun bajo bien cada cual cuando cuanto de del desde donde dos el ella ellas ellos en entre era es esa esas ese esos esta estas este esto estos fue ha hay la las le les lo los mas me mi mis muy no nos o para pero por porque puedo puedes que quien quiero se ser si sin sobre son su sus te tiene todo todos tu un una unas uno unos y ya necesito explica explicame explicar muestra mostrar buscar busca como cuales podemos hacer cual puedo quisiera favor".split())


class SearchFailure(Exception):
    pass


def sha_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize(word):
    return "".join(c for c in unicodedata.normalize("NFKD", word.casefold()) if not unicodedata.combining(c))


def query_terms(query):
    if not query.strip() or len(query) > 2000:
        raise SearchFailure("query_empty_or_too_long")
    terms = []
    for term in re.findall(r"\w+", query, re.UNICODE):
        canonical = normalize(term)
        if canonical not in STOPWORDS and canonical not in terms:
            terms.append(canonical)
    if len(terms) > 24:
        raise SearchFailure("query_too_many_search_terms")
    return terms


def build_index(chunks_path, report_path, destination):
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not report.get("complete") or report["output_sha256"] != sha_file(chunks_path):
        raise SearchFailure("chunk_report_incomplete_or_stale")
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(prefix="lexical-", suffix=".sqlite", dir=destination.parent, delete=False)
    staging = Path(handle.name)
    handle.close()
    count = 0
    try:
        with closing(sqlite3.connect(staging)) as db:
            db.executescript("""
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE chunks(rowid INTEGER PRIMARY KEY, chunk_id TEXT UNIQUE NOT NULL, text TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE citations(chunk_rowid INTEGER NOT NULL, subject TEXT NOT NULL, section TEXT NOT NULL, title TEXT NOT NULL, filename TEXT NOT NULL, method TEXT NOT NULL);
                CREATE INDEX citation_filters ON citations(subject, section, chunk_rowid);
                CREATE VIRTUAL TABLE passages USING fts5(body, title, section, tokenize='unicode61 remove_diacritics 2');
            """)
            with db:
                for line in chunks_path.read_text(encoding="utf-8").splitlines():
                    chunk = json.loads(line)
                    digest = hashlib.sha256(chunk["text"].encode()).hexdigest()
                    if chunk["text_sha256"] != digest or chunk["id"] != "chunk:" + digest or not chunk["citations"]:
                        raise SearchFailure("invalid_chunk_integrity")
                    cursor = db.execute("INSERT INTO chunks(chunk_id,text,payload) VALUES(?,?,?)", (chunk["id"], chunk["text"], json.dumps(chunk, ensure_ascii=False)))
                    rowid = cursor.lastrowid
                    titles, sections = [], []
                    for citation in chunk["citations"]:
                        source = citation["source"]
                        title, section = source["title"], source["section"]
                        titles.append(title + " " + source["filename"])
                        sections.append(section)
                        db.execute("INSERT INTO citations VALUES(?,?,?,?,?,?)", (rowid, source["subject"], section, title, source["filename"], citation["method"]))
                    db.execute("INSERT INTO passages(rowid,body,title,section) VALUES(?,?,?,?)", (rowid, chunk["text"], "\n".join(dict.fromkeys(titles)), "\n".join(dict.fromkeys(sections))))
                    count += 1
                db.executemany("INSERT INTO metadata VALUES(?,?)", [("schema_version", "1"), ("corpus_sha256", sha_file(chunks_path)), ("chunk_report_sha256", sha_file(report_path)), ("built_at", datetime.now(timezone.utc).isoformat()), ("chunks", str(count))])
            db.execute("INSERT INTO passages(passages) VALUES('integrity-check')")
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise SearchFailure("sqlite_integrity_failed")
        staging.replace(destination)
    finally:
        staging.unlink(missing_ok=True)
    print(json.dumps({"chunks": count, "database": str(destination), "bytes": destination.stat().st_size}))


class SearchEngine:
    def __init__(self, path=DEFAULT_DB):
        if not path.is_file():
            raise SearchFailure("missing_search_index")
        self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        self.db.row_factory = sqlite3.Row
        self.metadata = dict(self.db.execute("SELECT key,value FROM metadata"))
        if self.metadata.get("schema_version") != "1":
            self.close()
            raise SearchFailure("unsupported_index_version")

    def close(self):
        self.db.close()

    @staticmethod
    def citation_matches(citation, subject=None, section=None, native_only=False):
        source = citation["source"]
        return (subject is None or source["subject"] == subject) and (section is None or source["section"] == section) and (not native_only or citation["method"] == "native")

    def eligible(self, chunk_id, subject=None, section=None, native_only=False):
        row = self.db.execute("SELECT payload FROM chunks WHERE chunk_id=?", (chunk_id,)).fetchone()
        if row is None:
            return None
        chunk = json.loads(row[0])
        chunk["citations"] = [c for c in chunk["citations"] if self.citation_matches(c, subject, section, native_only)]
        return chunk if chunk["citations"] else None

    def all_chunks(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM chunks ORDER BY rowid")]

    def filters(self, subject, section, native_only):
        conditions, params = [], []
        if subject is not None:
            conditions.append("ci.subject=?")
            params.append(subject)
        if section is not None:
            conditions.append("ci.section=?")
            params.append(section)
        if native_only:
            conditions.append("ci.method='native'")
        condition = " AND ".join(conditions) if conditions else "1"
        return "EXISTS(SELECT 1 FROM citations ci WHERE ci.chunk_rowid=c.rowid AND " + condition + ")", params

    def lexical(self, query, limit=5, subject=None, section=None, native_only=False, exact=False):
        if limit < 1 or limit > 100:
            raise SearchFailure("invalid_result_limit")
        terms = query_terms(query)
        condition, params = self.filters(subject, section, native_only)
        if exact:
            rows = self.db.execute("SELECT c.chunk_id, 1.0 AS score, c.text AS excerpt FROM chunks c WHERE instr(c.text,?)>0 AND " + condition + " ORDER BY length(c.text), c.chunk_id LIMIT ?", [query, *params, limit]).fetchall()
            mode = "literal_case_sensitive"
        else:
            if not terms:
                raise SearchFailure("query_has_no_searchable_terms_use_exact")
            quoted = ['"' + word.replace('"', '""') + '"' for word in terms]
            rows = []
            for joiner, mode in [(" AND ", "all_terms"), (" OR ", "any_term")]:
                expression = joiner.join(quoted)
                rows = self.db.execute("SELECT c.chunk_id, -bm25(passages,1.0,1.5,0.4) AS score, snippet(passages,0,'[',']',' … ',36) AS excerpt FROM passages JOIN chunks c ON c.rowid=passages.rowid WHERE passages MATCH ? AND " + condition + " ORDER BY bm25(passages,1.0,1.5,0.4), c.chunk_id LIMIT ?", [expression, *params, limit]).fetchall()
                if rows:
                    break
        results = []
        for row in rows:
            chunk = self.eligible(row["chunk_id"], subject, section, native_only)
            excerpt = row["excerpt"]
            if exact:
                start = max(0, excerpt.index(query) - 120)
                excerpt = excerpt[start:start + len(query) + 300]
            results.append({"chunk_id": chunk["id"], "score": row["score"], "score_kind": "literal" if exact else "bm25", "lexical_mode": mode, "excerpt": excerpt, "requires_review": chunk["requires_review"], "warnings": chunk["warnings"], "citations": chunk["citations"]})
        return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--chunks", type=Path, default=ROOT / "data/aso/chunks/chunks.jsonl")
    build.add_argument("--report", type=Path, default=ROOT / "data/aso/chunks/report.json")
    build.add_argument("--database", type=Path, default=DEFAULT_DB)
    query = sub.add_parser("query")
    query.add_argument("query")
    query.add_argument("--database", type=Path, default=DEFAULT_DB)
    query.add_argument("--limit", type=int, default=5)
    query.add_argument("--subject")
    query.add_argument("--section")
    query.add_argument("--native-only", action="store_true")
    query.add_argument("--exact", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "build":
            build_index(args.chunks, args.report, args.database)
        else:
            engine = SearchEngine(args.database)
            try:
                print(json.dumps({"query": args.query, "results": engine.lexical(args.query, args.limit, args.subject, args.section, args.native_only, args.exact)}, ensure_ascii=False, indent=2))
            finally:
                engine.close()
        return 0
    except (SearchFailure, OSError, ValueError, KeyError, sqlite3.DatabaseError) as error:
        print(str(error) if isinstance(error, SearchFailure) else "No se ha podido procesar el índice local.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
