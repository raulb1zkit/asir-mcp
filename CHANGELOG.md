# Changelog

Todas las capacidades notables del código en este repositorio. El detalle
operativo y el historial de decisiones del proyecto se conservan en el
repositorio privado.

## Capacidades vigentes

- **Servidor MCP** (stdio y HTTP) con búsqueda híbrida (léxica + semántica)
  sobre PostgreSQL/pgvector, referencias verificables, límites por asignatura,
  rate limiting por token y métricas de peticiones.
- **Autenticación y autorización**: OAuth con Google (flujo PKCE y pantalla de
  consentimiento propia) y tokens personales independientes; el servidor
  autoriza cada petición contra una lista privada de UUID y asignaturas.
- **Ingesta y construcción**: registro validado de cursos con exclusiones,
  descarga de Moodle con sesión renovable, extracción de texto (incluidos
  DOCX y archivadores), OCR local con Tesseract, fragmentación con contexto de
  embeddings y reutilización de extracciones previas.
- **Publicación delta con CAS**: manifiestos inmutables, planes delta,
  esquema SQL de versionado con RLS, ledger de operaciones y sonda posterior
  antes de dar por buena la versión publicada.
- **Sincronización nocturna**: agente dedicado sin privilegios, delta
  obligatorio, preflight de disco, estado persistente y modo bootstrap de
  primera migración.
- **Respaldo cifrado**: paquetes del corpus con deduplicación, ciclo coordinado
  con el head publicado (CAS), retención local y remota tras validación,
  auditoría de uso de la cuenta de objetos y ensayos de restauración.
- **Monitorización**: collector de Docker + muestreo del host + exportación
  sanitizada, panel local y atlas de chunks, avisos Telegram (token y chat_id
  fuera de Git) y Worker externo de salud en Cloudflare con estado en KV.
- **Docker y CI**: imagen por allowlist con uid no privilegiado, compose de
  producción y overrides de prueba, CI pública sin secretos ni corpus real con
  fixtures sintéticas y smoke funcional (salud 200, 401 sin/ con token
  inválido), y despliegue manual por digest documentado.

## Convenciones

- Versión del esquema del corpus y del Worker: ver `docs/rag-versioning.md` y
  `deploy/cloudflare-health-worker/worker.js`.
- Las referencias de proyecto y origen son configurables por entorno con
  valores de ejemplo (`ASIR_SUPABASE_PROJECT_REF`, `ASIR_MOODLE_ORIGIN`, ...).
- `config/courses.json` es una plantilla sintética; los datos reales de un
  despliegue se mantienen fuera de este repositorio.
