#!/usr/bin/env bash
# Genera la configuración sintética para pruebas aisladas (CI y smoke local).
# No contiene credenciales reales: la CA y el certificado del "pooler" son
# autogenerados, el anfitrión solo existe como alias de red de prueba y el
# password es el valor sintético compartido con compose.pgci.yaml
# (${CI_PG_PASSWORD:-synthetic-ci-only}).
#
# Uso: make-fixtures.sh DIRECTORIO_FIXTURES DIRECTORIO_ESTADO
#      PYTHON=... make-fixtures.sh ...   (intérprete con requirements-mcp)
set -euo pipefail

fixtures="${1:?uso: make-fixtures.sh DIRECTORIO_FIXTURES DIRECTORIO_ESTADO}"
state="${2:?uso: make-fixtures.sh DIRECTORIO_FIXTURES DIRECTORIO_ESTADO}"
python_bin="${PYTHON:-python3}"
repo_root="$(cd "$(dirname "$0")/../../.." && pwd)"
password="${CI_PG_PASSWORD:-synthetic-ci-only}"

umask 077
mkdir -p "$fixtures" "$state"

# Certificado autofirmado que hace de CA y certificado del "pooler" sintético,
# con SAN ci.pooler.supabase.com para que verify-full valide el hostname.
openssl req -x509 -newkey rsa:2048 -days 3650 -nodes \
  -subj "/CN=ci-sintetico-ca" \
  -keyout "$fixtures/ci-ca-key.pem" -out "$fixtures/ci-ca.pem" >/dev/null 2>&1
openssl req -newkey rsa:2048 -nodes \
  -subj "/CN=ci.pooler.supabase.com" \
  -keyout "$fixtures/ci-server.key" -out "$fixtures/ci-server.csr" >/dev/null 2>&1
openssl x509 -req -days 3650 \
  -in "$fixtures/ci-server.csr" -CA "$fixtures/ci-ca.pem" -CAkey "$fixtures/ci-ca-key.pem" \
  -CAcreateserial \
  -extfile <(printf 'subjectAltName=DNS:ci.pooler.supabase.com') \
  -out "$fixtures/ci-server.crt" >/dev/null 2>&1
rm -f "$fixtures/ci-ca-key.pem" "$fixtures/ci-server.csr" "$fixtures/ci-ca.srl"

cat > "$fixtures/database.json" <<JSON
{
  "project_ref": "${ASIR_SUPABASE_PROJECT_REF:-your-project-ref}",
  "host": "ci.pooler.supabase.com",
  "port": 6543,
  "user": "asir_reader.${ASIR_SUPABASE_PROJECT_REF:-your-project-ref}",
  "password": "$password",
  "database": "postgres",
  "sslrootcert": "/etc/asir/ci-ca.pem"
}
JSON

# Token sintético: solo se conserva su SHA-256; el valor se descarta.
"$python_bin" "$repo_root/scripts/manage_tokens.py" --file "$fixtures/tokens.json" \
  create --label ci-sintetico --subjects ASO --scopes asir:read --rpm 30 --days 1 \
  >/dev/null

chmod 600 "$fixtures/ci-ca.pem" "$fixtures/ci-server.key" "$fixtures/ci-server.crt" \
  "$fixtures/database.json" "$fixtures/tokens.json"
printf 'fixtures sintéticos en %s (estado en %s)\n' "$fixtures" "$state"
