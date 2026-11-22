# Monitorización, panel y avisos

## Muestreo del servidor

`scripts/server_monitor.py` tiene tres subcomandos secuenciales (unidad
`asir-monitor.service`, Type=oneshot):

- `collector`: inspecciona el socket de Docker (solo root) y escribe un JSON
  contenedor por contenedor; es el único comando que toca el socket.
- `sample`: lee `/proc`, el SQLite propio y el JSON del collector; registra
  métricas por minuto en `metrics.sqlite`.
- `export`: construye el JSON sanitizado del panel (`resources.json`) con
  agregados diarios en `Europe/Madrid`; nunca incluye documentos ni secretos.

Las unidades aplican endurecimiento estándar y un `ReadWritePaths` mínimo; el
panel y el exportador jamás acceden al socket de Docker.

## Panel local

`dashboard/server.py` sirve el panel de operaciones en local con endpoints de
solo lectura; `dashboard/export_snapshot.py` exporta contadores sin documentos
ni secretos. `graph/` es el atlas local de chunks: consulta `/api/graph` del
servidor local y muestra la estructura del corpus sin exponer datos privados
en el HTML.

## Avisos Telegram

`scripts/telegram_alerts.py` evalúa cada 60 s (`asir-monitor-alerts.timer`) el
estado del monitor y envía incidencias, recuperación y un resumen diario. El
token y el `chat_id` se cargan desde un archivo privado (`telegram.json`),
nunca desde Git, imagen ni argv; los mensajes no incluyen contenido del corpus
ni URLs con token. Un fallo de configuración devuelve error sin enviar nada.

## Worker externo de salud

`deploy/cloudflare-health-worker/` contiene un Worker de Cloudflare que sondea
el endpoint público del MCP cada cinco minutos y avisa por Telegram cuando el
servicio es inaccesible desde fuera. Guarda su estado en KV (esquema
versionado), solo cuenta fallos consecutivos y tipos de incidente, y no expone
cuerpos ni secretos. Su despliegue (`deploy.py`) carga credenciales en memoria,
no las imprime ni las pasa por argv, y toca únicamente recursos con nombres
propios de este proyecto. `deploy/docker/ci/smoke.env` y la sonda local
corrigen los falsos positivos del propio muestreo.

Los valores reales de configuración (URL de salud, token, chat_id, cuenta de
Cloudflare) se provisionan por entorno y no forman parte de este repositorio.
