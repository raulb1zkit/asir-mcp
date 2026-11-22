-- Esquema sintético mínimo para las pruebas aisladas de CI/local.
-- No contiene corpus real: solo las filas que exigen el arranque del MCP
-- (metadatos e identidad de un corpus inexistente) y las tablas que
-- consulta /healthz para contar registros.
CREATE SCHEMA asir;

CREATE TABLE asir.corpus (
    id integer PRIMARY KEY,
    schema_version integer NOT NULL,
    corpus_sha256 text NOT NULL,
    chunker_version text,
    created_at timestamptz,
    report jsonb
);

INSERT INTO asir.corpus (id, schema_version, corpus_sha256, chunker_version, created_at, report)
VALUES (
    1,
    1,
    '0000000000000000000000000000000000000000000000000000000000000001',
    'sintetico',
    now(),
    '{}'::jsonb
);

CREATE TABLE asir.embedding_metadata (
    id integer PRIMARY KEY,
    model_fingerprint text,
    model_repository text,
    model_revision text,
    dimensions integer,
    max_tokens integer,
    overlap_tokens integer,
    context text,
    vectors_sha256 text,
    corpus_sha256 text NOT NULL
);

INSERT INTO asir.embedding_metadata (
    id, model_fingerprint, model_repository, model_revision, dimensions,
    max_tokens, overlap_tokens, context, vectors_sha256, corpus_sha256
)
VALUES (
    1,
    'sintetico',
    'intfloat/multilingual-e5-small',
    'sintetico',
    384,
    512,
    50,
    'sintetico',
    '0000000000000000000000000000000000000000000000000000000000000002',
    '0000000000000000000000000000000000000000000000000000000000000001'
);

-- /healthz solo cuenta filas de estas tablas.
CREATE TABLE asir.chunks (id integer PRIMARY KEY);
CREATE TABLE asir.embedding_windows (id integer PRIMARY KEY);
