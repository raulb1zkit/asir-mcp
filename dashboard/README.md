# Panel local de operaciones

Arranque desde la raíz con Python 3.9 o posterior:

```bash
python3 dashboard/server.py
```

Abrir http://127.0.0.1:8765. El panel lee `config/courses.json`, `data/sync/status.json` y, si es accesible, el contador SQLite del MCP. Para leer otras copias se pueden indicar `--state-dir` y `--metrics-db`; `--port` cambia el puerto. No copia archivos del VPS ni obtiene su estado automáticamente. Las cuentas de peticiones son HTTP, no usuarios ni consultas.

El servidor solo escucha en loopback y no implementa autenticación: no exponerlo a Internet ni mediante un proxy compartido. La respuesta limita los campos del estado y no incluye documentos, sesiones ni rutas. Un estado incompleto puede corresponder tanto a un proceso en curso como a uno interrumpido; la vista no comprueba systemd. Los datos ausentes se muestran como tales.

La ejecución desde la web, el seguimiento por asignatura y los despliegues necesitan una API de administración autenticada y autorizada, coordinación con el bloqueo existente, auditoría y un diseño de publicación parcial que conserve el corpus de las demás materias. El sincronizador actual procesa el registro completo; un botón no debe lanzar una publicación parcial sin resolver esa integración. El horario indicado es el de la plantilla versionada, no una comprobación del timer instalado.

## Servicio privado en el VPS

Las unidades en `deploy/dashboard/` separan el servidor web sin privilegios de una exportación periódica de resúmenes. El exportador lee el estado y el contador bajo root y escribe únicamente campos seleccionados en `/var/lib/asir-dashboard/status.json`; el panel no tiene acceso a los informes privados. La exportación y la página se actualizan automáticamente cada diez segundos; el contador puede tardar hasta unos veinte segundos en reflejar una petición. El estado por materia se obtiene del manifiesto de la ejecución identificada de forma inequívoca; en otros casos se muestra como desconocido. El número de elementos incluye los conservados y no equivale a archivos nuevos. La descarga completa tampoco garantiza publicación.

Desde el módulo de monitorización (docs/37), el exportador incrusta en `status.json` una sección `monitor` acotada (instantánea, avisos abiertos y agregados diarios de `/var/lib/asir-dashboard/resources.json`, que publica `scripts/server_monitor.py export`). El panel añade el endpoint `/api/resources` (solo lectura de ese JSON) y una vista de recursos del servidor con gráficas de 24 h limitadas a 1440 puntos; si el monitor no ha exportado aún, la sección se muestra como sin datos, nunca como ceros. El panel no accede al socket de Docker ni a SQLite.

El servicio del VPS escucha en `127.0.0.1:18767`. Tailscale Serve puede conectarlo a la red privada, sujeto a las reglas de acceso de la tailnet. El panel no dispone de autenticación propia ni de acciones de escritura: si se comparte mediante Serve, hay que restringir quién puede acceder mediante la política de Tailscale. Las unidades no habilitan botones de ejecución ni cambios en el sincronizador.
