# MCP-ASIR

Servidor MCP para recuperar materiales del ciclo ASIR desde una base de
documentos con búsqueda híbrida (léxica + semántica) sobre PostgreSQL/pgvector,
con referencias verificables y permisos por asignatura. Proyecto educativo de
infraestructura: ingesta nocturna desde Moodle, versionado del corpus,
respaldo cifrado y monitorización.

- [Arquitectura y operación](docs/arquitectura.md)
- [Docker, CI y despliegue](docs/docker-ci.md)
- [Versionado y publicación delta del corpus](docs/rag-versioning.md)
- [Respaldo cifrado y restauración](docs/backup-restauracion.md)
- [Monitorización, panel y avisos](docs/monitorizacion.md)
- [Registro de cursos y exclusiones](docs/registro-cursos.md)
- [Landing y pantalla de consentimiento](landing/README.md)

## Qué incluye este repositorio

- `scripts/`: sincronización, extracción/OCR, fragmentación, importación,
  servidor MCP (stdio y HTTP), autenticación (OAuth y tokens), búsqueda,
  evaluación, versionado delta, respaldo cifrado, monitorización y avisos.
- `tests/`: suite sintética completa, sin corpus real ni credenciales;
  incluye pruebas de PostgreSQL/pgvector aisladas (`tests/rag/`) y del Worker
  externo de salud (node:test).
- `deploy/`: compose, unidades systemd endurecidas, fixtures sintéticas de CI
  y el Worker externo de salud para Cloudflare.
- `dashboard/` y `graph/`: panel local de operaciones y atlas local de chunks.
- `landing/`: sitio estático de presentación y pantalla de consentimiento.
- `config/courses.json`: **plantilla sintética** del registro de cursos.

## Ejemplo de uso del servidor MCP local

Tras preparar los índices locales (corpus propio y modelo de embeddings):

```bash
.venv/bin/python scripts/semantic_search.py query "Cómo cambiar la prioridad de un proceso"
```

Comprobación del servidor MCP local, después de instalar `requirements-mcp.txt`:

```bash
.venv/bin/python scripts/check_mcp.py
```

La CI pública construye la imagen, valida los compose y ejecuta un smoke
funcional con fixtures sintéticas: salud 200, autorización 401 sin token y
métricas en el volumen con uid no privilegiado.

## Configuración

La configuración sensible (conexión a la base de datos, tokens, cookies de
Moodle, credenciales de respaldo y avisos) vive fuera de Git. Las referencias
de proyecto y origen aceptan variables de entorno con valores de ejemplo
(`ASIR_SUPABASE_PROJECT_REF`, `ASIR_MOODLE_ORIGIN`, ...); consulta
[docs/registro-cursos.md](docs/registro-cursos.md) y la
[política de seguridad](SECURITY.md). Algunas herramientas de evaluación
esperan datos privados que no se publican: con este repositorio solo no se
pueden ejecutar íntegramente.

## Estado

El servidor MCP remoto funciona detrás de Cloudflare, con búsqueda híbrida,
referencias y permisos por asignatura; la autenticación con Google requiere
aprobación individual y existen tokens personales independientes. La imagen de
producción se fija por digest y el respaldo cifrado y la sincronización delta
tienen sus temporizadores habilitados con guardas que fallan cerrado. Los
detalles operativos del despliegue real permanecen en el repositorio privado.
El historial de decisiones y etapas anteriores se conserva íntegro en el
repositorio privado; [CHANGELOG.md](CHANGELOG.md) resume las capacidades
vigentes de este código.
