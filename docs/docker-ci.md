# Docker, CI y despliegue

## Imagen

El `Dockerfile` construye la imagen del servidor MCP con un contexto por
allowlist (`.dockerignore` niega todo y reabre solo el software necesario), de
modo que el material educativo, los backups y las credenciales nunca entran en
la imagen. El contenedor corre con uid/gid no privilegiados y sin montajes de
datos privados; la CI comprueba estáticamente esas propiedades.

## Compose

- `deploy/docker/compose.yaml`: servicio del MCP; exige `ASIR_IMAGE` como
  referencia inmutable `ghcr.io/<owner>/mcp-asir@sha256:...`.
- `deploy/docker/compose.smoke.yaml`: override de prueba con salud en loopback.
- `deploy/docker/compose.fixtures.yaml`: fixtures sintéticos montados.
- `deploy/docker/compose.pgci.yaml`: PostgreSQL con pgvector y TLS que imita el
  endpoint del pooler (alias de red); sin corpus real ni credenciales reales.

## CI pública (`.github/workflows/ci.yml`)

La CI no usa corpus real, ni claves de Supabase o Moodle:

1. Tests del Worker externo de salud con `node --test` (solo builtins).
2. Suite Python sintética completa (`unittest discover`).
3. Publicación delta en PostgreSQL 17 + pgvector aislados (`tests/rag/run-postgres.sh`).
4. Validación estática de los cuatro compose.
5. Build de la imagen (carga local, sin publicar) y comprobaciones estáticas.
6. Smoke funcional: `healthz` 200 con recuento sintético, `POST /mcp` 401 sin
   token y 401 con token inválido, métricas en el volumen con uid correcto.

Los fixtures (`deploy/docker/ci/make-fixtures.sh` + `schema-synthetic.sql`)
generan la base mínima con datos claramente sintéticos; el token de prueba solo
se conserva como SHA-256.

## Workflow de imagen (`.github/workflows/image.yml`)

Build local y comprobaciones estáticas en cada cambio relevante de `main`. El
repositorio público **no publica** paquetes; la publicación y el despliegue
siguen su flujo operativo privado.

## Despliegue manual

El despliegue en el VPS es manual y por digest: consume una referencia de
imagen inmutable `ghcr.io/<owner>/mcp-asir@sha256:...`, sincroniza manifiestos
de compose y ejecuta un modo elegido:

- `smoke`: ensayo aislado en loopback.
- `bootstrap`: primera migración del venv del host.
- `production`: actualización de Docker con mantenimiento (termina en error 75
  si el mantenimiento sigue activo).

La mecánica completa (workflow de despliegue, clave SSH restringida y
`authorized_keys`) se gestiona en el repositorio operativo privado con sus
secretos; este repositorio solo documenta el diseño. La clave de despliegue
está restringida mediante `deploy/docker/asir-deploy-entry`, que solo acepta
verbos validados (`sync`, `run`) y descarta código por stdin; los helpers
privilegiados viven en el VPS, no en Git.

## Servicios systemd del host

`deploy/` incluye las unidades de sincronización nocturna (delta obligatorio,
agente dedicado sin privilegios), respaldo cifrado, monitorización, panel y
avisos, con endurecimiento estándar (`ProtectSystem=strict`, `ProtectHome`,
`NoNewPrivileges`, `ReadWritePaths` mínimos). Los temporizadores disparan el
respaldo tras cada ciclo correcto de publicación y evalúan avisos cada minuto.
Un fallo de backup no revierte el corpus publicado.
