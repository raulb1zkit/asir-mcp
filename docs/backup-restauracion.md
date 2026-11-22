# Respaldo cifrado y restauración

## Diseño

`scripts/rag_version_backup.py` coordina el manifiesto versionado del RAG con
un respaldo cifrado en almacenamiento de objetos (Cloudflare R2):

1. Refresca el uso de la cuenta antes de cada ciclo (con margen para actividad
   ajena y latencia de métricas de las APIs).
2. Respalda la versión head publicada: crea primero la copia local y sube los
   objetos cifrados; el commit del manifiesto va al final, solo después de
   comprobar toda la operación, incluidos reintentos, antes del primer PUT.
3. Ejecuta la retención local y remota solo si el backup y todos los pines se
   validan; un ciclo correcto sin cambios mantiene la versión vigente y limpia
   el aviso.

El catálogo (`init-catalog`) se crea tras verificar externamente que el prefijo
remoto es nuevo; `rag_storage.py` y `rag_backup.py` aportan el transporte, el
empaquetado determinista (gzip solo si reduce tamaño) y el inventario. El
respaldo es CAS-anotado sobre el mismo head y fase leídos al iniciar: si el
publicador cambió durante el backup, el snapshot se conserva y la operación no
pisar la versión publicada.

## Clave de recuperación

El respaldo cifrado requiere una clave de recuperación con copia fuera del
VPS. Sin esa copia externa el servicio de backup no debe activarse: perder la
clave impide restaurar. El token y la configuración nunca van en Git, imagen ni
argv.

## Ventanas y ensayos

La unidad `asir-rag-backup.service` corre tras cada ciclo correcto de
sincronización; la restauración usa una orden de ventanas ordenada (primero el
manifiesto, después los derivados) y se ensayó en entorno aislado antes de
habilitar nada en producción. `scripts/r2_account_usage.py` audita el uso de la
cuenta en cada ciclo. El empaquetado sintético de referencia (13k chunks,
27k vectores, manifiesto de 250 MB) cabe con margen en el límite de memoria de
la unidad.

Los detalles operativos concretos (prefijos reales, horarios exactos, incidentes
del proceso) permanecen en el repositorio privado con su historial íntegro.
