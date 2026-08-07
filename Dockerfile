FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.12-slim-bookworm

RUN useradd --create-home --uid 10001 app

COPY --from=builder --chown=app:app /app /app
RUN mkdir -p /app/data && chown app:app /app/data
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

USER app
WORKDIR /app
# Mounted at /app/data rather than /data so the default relative paths resolve the same
# way here as they do on a developer's machine, and one .env serves both.
VOLUME ["/app/data"]

HEALTHCHECK --interval=5m --timeout=10s --start-period=2m --retries=3 \
    CMD email-to-cal healthcheck || exit 1

ENTRYPOINT ["email-to-cal"]
CMD ["run"]
