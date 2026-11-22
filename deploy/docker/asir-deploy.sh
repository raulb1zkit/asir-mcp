#!/usr/bin/env bash
# Despliegue del MCP en el VPS por referencia inmutable (digest de GHCR).
#
# Se ejecuta como root en el VPS a través de la clave restringida de GitHub
# Actions: entry lee el token de stdin y ejecuta este helper instalado por admin.
# Subcomandos:
#   bootstrap <imagen@digest> <usuario> primera migración con recuperación venv
#   deploy <imagen@digest> <usuario-gh>  despliegue con retorno automático
#   smoke  <imagen@digest> <usuario-gh>  prueba aislada en 127.0.0.1:18080
#   rollback [imagen@digest] <usuario>   retorno a la referencia anterior
#   status                               estado sin secretos
#   install-unit                         instala la unidad envolvente (sin arrancar)
#
# El marcador /etc/asir/maintenance bloquea deploy, rollback e install-unit.
# smoke no toca el puerto 8080 ni el proyecto de producción.
set -euo pipefail
umask 077

PROJECT_DIR=${ASIR_PROJECT_DIR:-/opt/asir/mcp}
ENV_FILE="$PROJECT_DIR/.env"
ENV_FILE_PREV="$PROJECT_DIR/.env.previous"
LOG_FILE="$PROJECT_DIR/deploy.log"
MAINTENANCE_MARKER=${ASIR_MAINTENANCE_MARKER:-/etc/asir/maintenance}
HEALTH_URL=${ASIR_HEALTH_URL:-http://127.0.0.1:8080/healthz}
MCP_URL=${ASIR_MCP_URL:-http://127.0.0.1:8080/mcp}
SMOKE_PORT="${ASIR_SMOKE_PORT:-18080}"

log() { printf '%s [asir-deploy] %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$LOG_FILE"; }
die() { log "ERROR: $*"; exit 1; }

cd "$PROJECT_DIR"

guard_maintenance() {
  if [ -e "$MAINTENANCE_MARKER" ]; then
    log "mantenimiento activo ($MAINTENANCE_MARKER): operación NO ejecutada"
    exit 75
  fi
}

valid_ref() { [[ "${1:-}" =~ ^ghcr\.io/[a-z0-9._-]+/[a-z0-9._-]+@sha256:[0-9a-f]{64}$ ]]; }

current_image() { sed -n 's/^ASIR_IMAGE=//p' "$ENV_FILE" 2>/dev/null | head -1; }
previous_image() { sed -n 's/^ASIR_IMAGE=//p' "$ENV_FILE_PREV" 2>/dev/null | head -1; }

set_image_env() {
  local ref="$1" tmp
  tmp="$(mktemp "$PROJECT_DIR/.env.XXXXXX")"
  if [ -f "$ENV_FILE" ]; then
    if grep -q '^ASIR_IMAGE=' "$ENV_FILE"; then
      sed "s|^ASIR_IMAGE=.*|ASIR_IMAGE=$ref|" "$ENV_FILE" > "$tmp"
    else
      { cat "$ENV_FILE"; printf 'ASIR_IMAGE=%s\n' "$ref"; } > "$tmp"
    fi
  else
    printf 'ASIR_IMAGE=%s\nASIR_ENV_FILE=/etc/asir/mcp.env\n' "$ref" > "$tmp"
  fi
  chmod 600 "$tmp"
  mv "$tmp" "$ENV_FILE"
}

save_previous() { # <ref actual>
  local ref="$1"
  [ -n "$ref" ] || return 0
  printf 'ASIR_IMAGE=%s\n' "$ref" > "$ENV_FILE_PREV"
  chmod 600 "$ENV_FILE_PREV"
}

ghcr_login() { # <usuario>
  local user="$1"
  if [ -z "${ASIR_GHCR_TOKEN:-}" ] || [ "$ASIR_GHCR_TOKEN" = "-" ]; then
    die "falta el token GHCR en stdin"
  fi
  printf '%s' "$ASIR_GHCR_TOKEN" | docker login ghcr.io -u "$user" --password-stdin >/dev/null 2>&1 \
    || die "login GHCR fallido"
  ASIR_GHCR_TOKEN=""
}

ghcr_logout() { docker logout ghcr.io >/dev/null 2>&1 || true; }

pull_ref() { # <ref> <usuario>
  ghcr_login "$2"
  if ! docker pull "$1" >/dev/null; then
    ghcr_logout
    die "pull fallido de $1"
  fi
  ghcr_logout
  log "imagen descargada: $1"
}

http_status() { # <GET|POST> <url>
  python3 - "$1" "$2" <<'PY'
import sys
import urllib.error
import urllib.request

method, url = sys.argv[1], sys.argv[2]
data = b"{}" if method == "POST" else None
request = urllib.request.Request(
    url,
    data=data,
    method=method,
    headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    },
)
try:
    with urllib.request.urlopen(request, timeout=5) as response:
        print(response.status)
except urllib.error.HTTPError as error:
    print(error.code)
except Exception:
    print(0)
PY
}

http_body() { # <url>
  python3 - "$1" <<'PY'
import sys
import urllib.error
import urllib.request

try:
    with urllib.request.urlopen(sys.argv[1], timeout=5) as response:
        sys.stdout.write(response.read().decode("utf-8", "replace"))
except urllib.error.HTTPError as error:
    sys.stdout.write(error.read().decode("utf-8", "replace"))
except Exception as error:
    print(f"sin respuesta: {type(error).__name__}")
PY
}

wait_status() { # <url> <esperado> <intentos>
  local url="$1" expected="$2" attempts="${3:-30}" code=0 i
  for ((i = 1; i <= attempts; i++)); do
    code="$(http_status GET "$url")"
    if [ "$code" = "$expected" ]; then
      return 0
    fi
    sleep 2
  done
  log "estado final de $url: $code (esperado $expected)"
  return 1
}

verify_401() { # <url>
  local code
  code="$(http_status POST "$1")"
  if [ "$code" = "401" ]; then
    return 0
  fi
  log "POST $1 sin token devolvió $code (esperado 401)"
  return 1
}

verify_public() {
  wait_status "$HEALTH_URL" 200 30 || return 1
  log "salud: $(http_body "$HEALTH_URL")"
  verify_401 "$MCP_URL" || return 1
  log "autorización 401 correcta en $MCP_URL"
}

compose() { docker compose -f "$PROJECT_DIR/compose.yaml" "$@"; }

compose_up_wait() { # <timeout>
  compose up -d --wait --wait-timeout "${1:-90}" mcp
}

cmd_deploy() {
  local ref="${1:-}" actor="${2:-}" prev
  guard_maintenance
  valid_ref "$ref" || die "referencia inválida: se espera ghcr.io/.../...@sha256:<64 hex>"
  [ -n "$actor" ] || die "falta el usuario de GHCR"
  prev="$(current_image || true)"
  valid_ref "$prev" || die "primer despliegue: usa bootstrap para conservar recuperación a la unidad venv"
  pull_ref "$ref" "$actor"
  if [ "$prev" != "$ref" ]; then
    save_previous "$prev"
    set_image_env "$ref"
  fi
  log "levantando $ref (anterior: ${prev:-ninguna})"
  if compose_up_wait 90 && verify_public; then
    log "DESPLIEGUE OK: $ref"
    return 0
  fi
  log "fallo verificado: se restaura $prev"
  set_image_env "$prev"
  if compose_up_wait 90 && verify_public; then
    die "rollback OK sobre $prev; el despliegue de $ref queda como fallido"
  fi
  die "rollback NO verificado sobre $prev: intervención manual requerida"
}

# Primera migración: fallback explícito a la unidad venv, no a un digest ficticio.
cmd_bootstrap() {
  local ref="${1:-}" actor="${2:-}"
  local unit=${ASIR_SYSTEMD_DIR:-/etc/systemd/system}/asir-mcp.service
  local backup="$unit.venv-previo"
  guard_maintenance
  valid_ref "$ref" || die "referencia inválida"
  [ -n "$actor" ] || die "falta usuario GHCR"
  [ ! -e "$ENV_FILE" ] || die "bootstrap exige ausencia de .env; usa deploy"
  grep -q '/opt/asir/venv' "$unit" || die "bootstrap exige la unidad venv original"
  [ -x "${ASIR_VENV_PYTHON:-/opt/asir/venv/bin/python}" ] || die "venv de recuperación ausente"
  if [ -e "$backup" ]; then
    cmp -s "$unit" "$backup" || die "backup venv difiere de la unidad actual; revisar antes de bootstrap"
  fi
  if systemctl is-active --quiet asir-mcp.service; then
    die "bootstrap exige MCP inactivo antes de sustituir su unidad"
  fi
  pull_ref "$ref" "$actor"
  if cmd_install_unit && set_image_env "$ref" && systemctl start asir-mcp.service && verify_public; then
    log "BOOTSTRAP OK: $ref; recuperación venv conservada en $backup"
    return 0
  fi
  log "bootstrap fallido: restaurando unidad venv"
  systemctl stop asir-mcp.service || true
  if [ -e "$ENV_FILE" ]; then
    compose down --remove-orphans || die "no se pudo detener Docker; intervención manual"
    mv "$ENV_FILE" "$PROJECT_DIR/.env.bootstrap-failed"
  fi
  cp -p "$backup" "$unit"
  systemctl daemon-reload
  if systemctl start asir-mcp.service && verify_public; then
    die "bootstrap fallido; venv restaurado y verificado"
  fi
  die "recuperación venv NO verificada; intervención manual"
}

cmd_rollback() {
  local ref="${1:-}" actor="${2:-}" current
  guard_maintenance
  current="$(current_image)"
  [ -n "$ref" ] || ref="$(previous_image)"
  valid_ref "$ref" || die "referencia de retorno inválida"
  [ -n "$actor" ] || die "falta el usuario de GHCR"
  [ -n "$current" ] || die "no hay imagen actual registrada"
  pull_ref "$ref" "$actor"
  save_previous "$current"
  set_image_env "$ref"
  log "retorno solicitado a $ref (anterior: $current)"
  if compose_up_wait 90 && verify_public; then
    log "RETORNO OK: $ref"
    return 0
  fi
  log "fallo del retorno: se restaura la referencia previa $current"
  set_image_env "$current"
  if compose_up_wait 90 && verify_public; then
    die "retorno fallido; la imagen previa $current quedó restaurada y verificada"
  fi
  die "retorno fallido y restauración de $current no verificada: intervención manual requerida"
}

cmd_smoke() {
  local ref="${1:-}" actor="${2:-}" cid
  valid_ref "$ref" || die "referencia inválida"
  [ -n "$actor" ] || die "falta el usuario de GHCR"
  pull_ref "$ref" "$actor"
  log "smoke aislado en 127.0.0.1:$SMOKE_PORT (sin ruta pública)"
  # Variables explícitas: el smoke no lee ni modifica el .env de producción.
  export ASIR_IMAGE="$ref"
  export ASIR_ENV_FILE=/etc/asir/mcp.env
  export ASIR_SMOKE_PORT="$SMOKE_PORT"
  local smoke=(docker compose -p asir-mcp-smoke
    -f "$PROJECT_DIR/compose.yaml" -f "$PROJECT_DIR/compose.smoke.yaml")
  if ! "${smoke[@]}" up -d --wait --wait-timeout 120 mcp; then
    "${smoke[@]}" logs --tail 80 mcp | tee -a "$LOG_FILE" || true
    "${smoke[@]}" down --remove-orphans >/dev/null 2>&1 || true
    die "el contenedor de smoke no alcanzó salud correcta"
  fi
  if ! wait_status "http://127.0.0.1:$SMOKE_PORT/healthz" 200 15; then
    "${smoke[@]}" logs --tail 80 mcp | tee -a "$LOG_FILE" || true
    "${smoke[@]}" down --remove-orphans >/dev/null 2>&1 || true
    die "healthz del smoke no respondió 200"
  fi
  log "salud: $(http_body "http://127.0.0.1:$SMOKE_PORT/healthz")"
  verify_401 "http://127.0.0.1:$SMOKE_PORT/mcp" || {
    "${smoke[@]}" down --remove-orphans >/dev/null 2>&1 || true
    die "el smoke no devolvió 401 sin token"
  }
  cid="$("${smoke[@]}" ps -q mcp)"
  log "contenedor: uid=$(docker exec "$cid" id -u) gid=$(docker exec "$cid" id -g)"
  log "escucha: $(ss -ltn | awk '$4 ~ /'"$SMOKE_PORT"'$/ {print $4}' | tr '\n' ' ')"
  "${smoke[@]}" down --remove-orphans >/dev/null 2>&1 || true
  log "SMOKE OK: $ref en 127.0.0.1:$SMOKE_PORT (contenedor detenido)"
}

cmd_status() {
  log "imagen actual: $(current_image || true)"
  log "imagen de retorno: $(previous_image || true)"
  if valid_ref "$(current_image || true)"; then
    docker compose -f "$PROJECT_DIR/compose.yaml" ps || true
  else
    log "sin imagen de producción configurada (pre-bootstrap)"
  fi
  docker image ls --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.CreatedSince}}' | grep -i mcp-asir || true
  if [ -e "$MAINTENANCE_MARKER" ]; then
    log "marcador de mantenimiento: PRESENTE"
  else
    log "marcador de mantenimiento: ausente"
  fi
}

cmd_install_unit() {
  local unit=${ASIR_SYSTEMD_DIR:-/etc/systemd/system}/asir-mcp.service
  local backup="$unit.venv-previo"
  guard_maintenance
  if [ -f "$unit" ] && [ ! -f "$backup" ] \
      && grep -q '/opt/asir/venv' "$unit"; then
    cp -p "$unit" "$backup" || return 1
    log "unidad previa respaldada en $backup"
  fi
  install -m 644 "$PROJECT_DIR/asir-mcp.service" "$unit" || return 1
  systemctl daemon-reload || return 1
  log "unidad envolvente instalada (sin arrancar; el marcador de mantenimiento sigue controlando el arranque)"
}

case "${1:-}" in
  deploy|production)
    cmd_deploy "${2:-}" "${3:-}"
    ;;
  smoke)
    cmd_smoke "${2:-}" "${3:-}"
    ;;
  bootstrap)
    cmd_bootstrap "${2:-}" "${3:-}"
    ;;
  rollback)
    cmd_rollback "${2:-}" "${3:-}"
    ;;
  status)
    cmd_status
    ;;
  install-unit)
    cmd_install_unit
    ;;
  *)
    echo "uso: deploy|bootstrap|smoke|rollback|status|install-unit [argumentos]" >&2
    exit 2
    ;;
esac
