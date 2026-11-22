-- Preparación administrativa explícita, privada; no ejecutar en producción automáticamente.
CREATE TABLE IF NOT EXISTS asir.rag_head (
 id smallint PRIMARY KEY CHECK (id=1), version text NOT NULL, corpus_id text NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS asir.rag_operations (
 operation_id text PRIMARY KEY, base_version text NOT NULL, target_version text NOT NULL,
 status text NOT NULL CHECK (status='committed_pending_probe'),
 committed_at timestamptz NOT NULL DEFAULT now(), checks jsonb NOT NULL
);
REVOKE ALL ON asir.rag_head, asir.rag_operations FROM PUBLIC;
ALTER TABLE asir.rag_head ENABLE ROW LEVEL SECURITY;
ALTER TABLE asir.rag_operations ENABLE ROW LEVEL SECURITY;
CREATE OR REPLACE FUNCTION asir.rag_canonical(v jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE SET search_path=pg_catalog AS $$
DECLARE result text;
BEGIN
 CASE jsonb_typeof(v)
 WHEN 'object' THEN
  SELECT '{'||coalesce(string_agg(to_jsonb(key)::text||':'||asir.rag_canonical(value), ',' ORDER BY key COLLATE "C"),'')||'}'
    INTO result FROM jsonb_each(v);
 WHEN 'array' THEN
  SELECT '['||coalesce(string_agg(asir.rag_canonical(value), ',' ORDER BY n),'')||']'
    INTO result FROM jsonb_array_elements(v) WITH ORDINALITY a(value,n);
 ELSE result := v::text;
 END CASE;
 RETURN result;
END $$;
CREATE OR REPLACE FUNCTION asir.rag_hash(v jsonb) RETURNS text
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE SET search_path=pg_catalog AS $$
 SELECT encode(sha256(convert_to(asir.rag_canonical(v),'UTF8')), 'hex')
$$;
-- Proyección de valores reales. No usar hashes suministrados como prueba del estado.
-- Fuentes y ocurrencias usan listas de campos fijas (8/10, iguales a las filas del
-- manifiesto): una columna futura en las tablas no puede cambiar el hash canónico
-- en silencio; exigiría migración coordinada (invariante documentado en docs/44).
-- El prefijo de cita se preagrega por chunk y los segmentos por referencia en CTEs:
-- evita la subconsulta correlacionada por ventana sin cambiar un byte del payload.
CREATE OR REPLACE VIEW asir.rag_rows WITH (security_invoker=true) AS
 WITH citation_prefix AS (
  SELECT ci.chunk_id,
   min(('passage: '||s.subject||' | '||left(s.section,120)||' | '||left(s.title,160)||E'\n') COLLATE "C") AS min_prefix,
   (array_agg('passage: '||s.subject||' | '||left(s.section,120)||' | '||left(s.title,160)||E'\n' ORDER BY ci.ordinal))[1] AS first_prefix
  FROM asir.citations ci JOIN asir.citation_sources s ON s.citation_id=ci.id
  GROUP BY ci.chunk_id
 ), reference_segments AS (
  SELECT s.citation_id, jsonb_agg(jsonb_build_object('source_unit_id',s.source_unit_id,'locator',s.locator,
   'start',s.start_char,'end',s.end_char,'method',s.method,'quality',s.quality) ORDER BY s.ordinal) AS segments
  FROM asir.citation_segments s GROUP BY s.citation_id
 )
 SELECT 'sources'::text AS kind, s.sha256 AS physical_id, jsonb_build_object(
  'sha256',s.sha256,'subject',s.subject,'section',s.section,'title',s.title,
  'filename',s.filename,'url',s.url,'mime',s.mime,'path',s.path) AS payload FROM asir.sources s
 UNION ALL
 SELECT 'occurrences', o.source_key, jsonb_build_object(
  'sha256',o.sha256,'subject',o.subject,'section',o.section,'title',o.title,
  'filename',o.filename,'url',o.url,'mime',o.mime,'path',o.path,
  'source_key',o.source_key,'moodle_active',o.moodle_active) FROM asir.source_occurrences o
 UNION ALL
 SELECT 'chunks', c.id, jsonb_build_object(
  'id',c.id,'text',c.text,'text_sha256',c.text_sha256,'characters',c.characters,
  'warnings',c.warnings,'requires_review',c.requires_review,
  'titles',(SELECT jsonb_agg(x ORDER BY x COLLATE "C") FROM (SELECT DISTINCT unnest(string_to_array(c.titles,E'\n')) AS x) q),
  -- Contrato de secciones vacías: el importador almacena '' cuando la única línea
  -- de sección es vacía (string_to_array('', E'\n') es un array de 0 elementos,
  -- mientras el manifiesto canónico representa la misma fila como ['']). El CASE
  -- corrige solo esa proyección, sin DML de corpus; titles no puede ser '' porque
  -- la fórmula del importador (título + ' ' + filename) siempre contiene un espacio.
  'sections', CASE WHEN c.sections = '' THEN jsonb_build_array(''::text)
    ELSE (SELECT jsonb_agg(x ORDER BY x COLLATE "C") FROM (SELECT DISTINCT unnest(string_to_array(c.sections,E'\n')) AS x) q) END
 ) FROM asir.chunks c
 UNION ALL
 SELECT 'references', c.id::text, jsonb_build_object(
  'chunk_id',c.chunk_id,'source_sha256',c.source_sha256,'source_key',c.source_key,
  'method',c.method,'previous_chunk_id',c.previous_chunk_id,'next_chunk_id',c.next_chunk_id,
  'segments', coalesce(r.segments,'[]'::jsonb)
 ) FROM asir.citations c LEFT JOIN reference_segments r ON r.citation_id=c.id
 UNION ALL
 SELECT 'vectors', w.id::text, jsonb_build_object(
  'chunk_id',w.chunk_id,'char_start',w.char_start,'char_end',w.char_end,'tokens',w.tokens,
  'input_sha256',encode(sha256(convert_to((
   CASE WHEN m.context='stable-subject-section-title-v2' THEN p.min_prefix ELSE p.first_prefix END
  ) || substring(c.text FROM w.char_start+1 FOR w.char_end-w.char_start),'UTF8')),'hex'),
  'vector_hex',encode(substring(extensions.vector_send(w.embedding) FROM 5),'hex'),
  'config_hash',asir.rag_hash(jsonb_build_object('model_fingerprint',m.model_fingerprint,
   'model_repository',m.model_repository,'model_revision',m.model_revision,'dimensions',m.dimensions,
   'max_tokens',m.max_tokens,'overlap_tokens',m.overlap_tokens,'context',m.context))
 ) FROM asir.embedding_windows w JOIN asir.chunks c ON c.id=w.chunk_id
   JOIN citation_prefix p ON p.chunk_id=w.chunk_id CROSS JOIN asir.embedding_metadata m
 UNION ALL
 SELECT 'config', 'embedding', jsonb_build_object('model_fingerprint',m.model_fingerprint,
   'model_repository',m.model_repository,'model_revision',m.model_revision,'dimensions',m.dimensions,
   'max_tokens',m.max_tokens,'overlap_tokens',m.overlap_tokens,'context',m.context)
 FROM asir.embedding_metadata m
 UNION ALL
 SELECT 'config', 'pipeline', jsonb_build_object('schema_version',c.schema_version,
   'chunker_version',c.chunker_version,'config',coalesce(c.report->'config','{}'::jsonb))
 FROM asir.corpus c;
REVOKE ALL ON asir.rag_rows FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION asir.rag_canonical(jsonb), asir.rag_hash(jsonb) FROM PUBLIC;
-- Defensa en profundidad: el publicador solo ve estas tablas privadas mediante rol fijo.
DO $$ BEGIN
 IF EXISTS (SELECT FROM pg_roles WHERE rolname='asir_ingest') THEN
  GRANT SELECT, INSERT, UPDATE ON asir.rag_head TO asir_ingest;
  GRANT SELECT, INSERT ON asir.rag_operations TO asir_ingest;
  GRANT SELECT ON asir.rag_rows TO asir_ingest;
  GRANT EXECUTE ON FUNCTION asir.rag_canonical(jsonb), asir.rag_hash(jsonb) TO asir_ingest;
  DROP POLICY IF EXISTS rag_head_ingest ON asir.rag_head;
  CREATE POLICY rag_head_ingest ON asir.rag_head TO asir_ingest USING (true) WITH CHECK (true);
  DROP POLICY IF EXISTS rag_ops_ingest ON asir.rag_operations;
  CREATE POLICY rag_ops_ingest ON asir.rag_operations TO asir_ingest USING (true) WITH CHECK (true);
  -- Esquema antiguo ya concede DML y USAGE de secuencias; no requiere TRUNCATE.
 END IF;
END $$;

DO $$ BEGIN
 IF EXISTS (SELECT FROM pg_roles WHERE rolname='asir_reader') THEN
  GRANT SELECT ON asir.rag_head TO asir_reader;
  DROP POLICY IF EXISTS rag_head_reader ON asir.rag_head;
  CREATE POLICY rag_head_reader ON asir.rag_head FOR SELECT TO asir_reader USING (true);
 END IF;
END $$;
