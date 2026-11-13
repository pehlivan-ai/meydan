# Multi-stage build following https://docs.astral.sh/uv/guides/integration/docker
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9.10 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
# git fetches git dependencies (meydan); builder stage only, not in the final image.
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev

FROM python:3.12-slim
RUN useradd --system --create-home app
WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app alembic.ini /app/
COPY --chown=app:app migrations /app/migrations
COPY --chown=app:app app /app/app
ENV PATH="/app/.venv/bin:$PATH"
USER app
EXPOSE 8000
# Migrations are not part of the start: the compose service `migrate` (python -m app.migrate)
# runs them first, as a deploy step, so a rollback starts the previous image without migrating.
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8000"]
