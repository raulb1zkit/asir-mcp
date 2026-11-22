#!/usr/bin/env bash
# Base desechable aislada, sin credenciales/volúmenes/configuración de producción.
set -euo pipefail
name="asir-rag-test-${RANDOM}-${RANDOM}"
image="${ASIR_RAG_TEST_IMAGE:-pgvector/pgvector:0.8.2-pg16-bookworm}"
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT
docker run -d --name "$name" -e POSTGRES_HOST_AUTH_METHOD=trust -p 127.0.0.1::5432 "$image" >/dev/null
for attempt in {1..40}; do
  if docker exec "$name" pg_isready -h 127.0.0.1 -U postgres >/dev/null 2>&1; then break; fi
  sleep .25
done
port=$(docker port "$name" 5432/tcp | sed 's/.*://')
export ASIR_RAG_TEST_DSN="host=127.0.0.1 port=$port user=postgres dbname=postgres"
"${RAG_TEST_PYTHON:-python}" -m unittest tests.test_rag_versions tests.test_rag_postgres tests.test_rag_reconcile -v
