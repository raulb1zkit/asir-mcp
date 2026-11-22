# Versionado y publicación delta del corpus

## Manifiestos inmutables

`scripts/rag_versions.py` construye los manifiestos del corpus canónico y los
planes delta (sin modelos ni red): cada versión es un artefacto verificable con
rutas y hashes fuera de la identidad, y una versión se crea una sola vez. La
canonicalización JSON y el hash usan la función estable de
`scripts/sql/rag_versioning.sql` (esquema `asir.rag_head`, `rag_operations` con
RLS y `REVOKE` a `PUBLIC`).

## Publicación delta

`scripts/rag_publish.py` publica en PostgreSQL con CAS (compare-and-swap): una
conexión nueva distingue commit confirmado, no aplicado y estado ajeno, y
regresa con error si el head cambió durante la operación. Cada operación queda
en un ledger con su estado `committed_pending_probe` y sus comprobaciones; la
sonda posterior confirma el servicio antes de dar por buena la versión.

La primera migración se instala con el modo `--bootstrap` contra la copia
publicada; el modo legado completo (`--legacy-full-publish`) queda reservado y
el sincronizador falla cerrado si no se pide explícitamente un modo válido.

## Paquetes del corpus

`scripts/rag_bundle.py` genera paquetes deduplicables del corpus canónico, sin
rutas privadas ni índices monolíticos; 64 particiones estables por tabla hacen
que una edición no invalide todo el corpus.

## Sincronización nocturna

`scripts/sync_materials.py` ejecuta descarga fresca, construcción y ensayo
privado; con `--rag-delta-publish` publica solo el delta y exige banderas
explícitas (`--staging`, `--scheduled`, `--publish`, `--rollback-candidate`,
`--probe-token-file`, `--rag-resource-map`) o falla antes de construir. El
preflight bloquea una construcción nueva si el disco no tiene margen, y el
estado (usuario dedicado, sin privilegios) registra cada ciclo en
`rag-status.json`.

## Retención y archivado

`scripts/archive_old_corpora.py` respalda los corpus históricos y permite
retirarlos tras validar el archivo; las ausencias solo se conservan tras un
inventario completo, y una revisión nueva sustituye al original consultable.
Los acuerdos de recursos y retención (umbrales de disco, presupuestos) se
documentaron en el repositorio operativo y conservan su historial privado.
