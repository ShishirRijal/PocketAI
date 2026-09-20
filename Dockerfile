FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra postgres
COPY README.md alembic.ini ./
COPY config ./config
COPY src ./src
COPY deploy ./deploy
RUN uv sync --frozen --no-dev --extra postgres

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends sqlite3 curl tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 pocket
WORKDIR /app
COPY --from=build --chown=pocket:pocket /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    POCKET_ENV=prod \
    POCKET_LOG_JSON=true \
    POCKET_DATABASE_URL=sqlite:////data/pocket.db \
    POCKET_BACKUP_DIR=/data/backups \
    POCKET_EXPORT_DIR=/data/exports
RUN mkdir -p /data && chown pocket:pocket /data
USER pocket
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD curl -fsS http://localhost:8080/healthz || exit 1
ENTRYPOINT ["tini", "--", "/app/deploy/entrypoint.sh"]
CMD ["api"]
