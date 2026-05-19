# ── API Builder ───────────────────────────────────────────────────────────────
FROM python:3.13-alpine AS api-builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

ENV PATH="$UV_PROJECT_ENVIRONMENT/bin:$PATH"

RUN apk add --no-cache \
    ca-certificates \
    build-base \
    linux-headers \
    libffi-dev \
    openssl-dev \
    curl-dev \
    cargo \
    rust

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.6 /uv /uvx /bin/
COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-dev --no-install-project \
    && find /opt/venv -type d \
         \( -name "__pycache__" -o -name "tests" -o -name "test" -o -name "testing" \) \
         -prune -exec rm -rf {} + \
    && find /opt/venv -type f -name "*.pyc" -delete \
    && (find /opt/venv -type f -name "*.so" -exec strip --strip-unneeded {} + 2>/dev/null || true) \
    && rm -rf /root/.cache /tmp/uv-cache


# ── API Runtime ───────────────────────────────────────────────────────────────
FROM python:3.13-alpine AS api-runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    VIRTUAL_ENV=/opt/venv \
    SERVER_HOST=0.0.0.0 \
    SERVER_PORT=8000 \
    SERVER_WORKERS=1

ENV PATH="$VIRTUAL_ENV/bin:$PATH"

RUN apk add --no-cache \
    tzdata \
    ca-certificates \
    libffi \
    openssl \
    libgcc \
    libstdc++ \
    libcurl

WORKDIR /app

COPY --from=api-builder /opt/venv /opt/venv
COPY pyproject.toml config.defaults.toml maintainer.config.example.json ./
COPY app ./app
COPY scripts ./scripts

RUN mkdir -p /app/data /app/logs \
    && chmod +x /app/scripts/entrypoint.sh /app/scripts/init_storage.sh /app/scripts/maintainer-entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["sh", "-c", "wget -qO /dev/null http://127.0.0.1:${SERVER_PORT}/health || exit 1"]

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["sh", "-c", "exec granian --interface asgi --host ${SERVER_HOST} --port ${SERVER_PORT} --workers ${SERVER_WORKERS} app.main:app"]


# ── Maintainer Builder ────────────────────────────────────────────────────────
FROM python:3.13-slim AS maintainer-builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

ENV PATH="$UV_PROJECT_ENVIRONMENT/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.6 /uv /uvx /bin/
COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-dev --extra maintainer --no-install-project \
    && find /opt/venv -type d \
         \( -name "__pycache__" -o -name "tests" -o -name "test" -o -name "testing" \) \
         -prune -exec rm -rf {} + \
    && find /opt/venv -type f -name "*.pyc" -delete \
    && rm -rf /root/.cache /tmp/uv-cache


# ── Maintainer Runtime ────────────────────────────────────────────────────────
FROM python:3.13-slim AS maintainer-runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    VIRTUAL_ENV=/opt/venv \
    CHROME_BIN=/usr/bin/chromium \
    MAINTAINER_HEADLESS=false \
    MAINTAINER_USE_XVFB=true \
    MAINTAINER_NO_SANDBOX=true \
    MAINTAINER_DISABLE_DEV_SHM=true

ENV PATH="$VIRTUAL_ENV/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        fonts-liberation \
        libasound2 \
        libatk-bridge2.0-0 \
        libgbm1 \
        libgtk-3-0 \
        libnss3 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        tzdata \
        wget \
        xdg-utils \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=maintainer-builder /opt/venv /opt/venv
COPY pyproject.toml config.defaults.toml maintainer.config.example.json ./
COPY app ./app
COPY scripts ./scripts

RUN mkdir -p /app/data /app/logs \
    && chmod +x /app/scripts/entrypoint.sh /app/scripts/init_storage.sh /app/scripts/maintainer-entrypoint.sh

ENTRYPOINT ["/app/scripts/maintainer-entrypoint.sh"]


# Keep the default docker build output aligned with the API image.
FROM api-runtime AS final
