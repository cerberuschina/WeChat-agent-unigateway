# The gateway is stdlib-only; this image adds nothing but a pinned interpreter.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# No pip install: the project has no dependencies.
COPY . /app

# Config is mounted read-only at /config; everything the gateway writes lives in
# /app/data (account.json, virtual/binds.json, media, token files).
RUN useradd --system --uid 10001 --home /app gateway \
    && mkdir -p /app/data \
    && chown -R gateway:gateway /app/data

USER gateway

EXPOSE 18500 18600

# Loopback probe: admin_cidrs must keep 127.0.0.1/32 for this to answer.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:18500/health', timeout=3).status==200 else 1)"]

ENTRYPOINT ["python", "-m", "agent_gateway", "--config", "/config/gateway.json"]
