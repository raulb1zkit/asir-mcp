# syntax=docker/dockerfile:1
# Imagen del MCP de ASIR: solo software y dependencias fijadas.
# El contexto se limita con .dockerignore (allowlist): no entran materiales,
# backups, sesiones ni credenciales. La configuración y el modelo E5 se
# montan desde el VPS en tiempo de ejecución.

# Base fijada por digest verificado (índice multiplataforma de python:3.14-slim).
FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f

# onnxruntime necesita libgomp1; ca-certificates para TLS (pooler y JWKS).
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates libgomp1; \
    rm -rf /var/lib/apt/lists/*

# Usuario sin privilegios equivalente al del VPS (asir 999:983 por defecto),
# para que los archivos montados conserven su propietario real.
ARG ASIR_UID=999
ARG ASIR_GID=983
RUN set -eu; \
    groupadd --gid "${ASIR_GID}" asir; \
    useradd --uid "${ASIR_UID}" --gid "${ASIR_GID}" --home-dir /var/lib/asir \
      --no-create-home --shell /usr/sbin/nologin asir

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/var/lib/asir

WORKDIR /opt/asir/app

COPY requirements-mcp.txt ./
RUN pip install --no-cache-dir -r requirements-mcp.txt

COPY scripts/ ./scripts/

USER asir

EXPOSE 8080

# Salud estricta: 200 solo con base consultable; un 503 (degradado) no es sano.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import sys,urllib.request as u; r=u.urlopen('http://127.0.0.1:8080/healthz', timeout=5); sys.exit(0 if r.status == 200 else 1)"]

CMD ["python", "scripts/mcp_http.py"]
