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

# The commit this image was built from, stamped in by the release workflow so the
# settings page can compare the running build with the registry's newest and offer an
# update. Empty in a local build, which the page reads as "cannot say".
ARG GIT_SHA=""
ENV EMAIL_TO_CAL_BUILD_SHA=$GIT_SHA

USER app
WORKDIR /app
# Mounted at /app/data rather than /data so the default relative paths resolve the same
# way here as they do on a developer's machine, and one configuration serves both.
VOLUME ["/app/data"]

EXPOSE 8080
HEALTHCHECK --interval=5m --timeout=10s --start-period=2m --retries=3 \
    CMD email-to-cal healthcheck || exit 1

ENTRYPOINT ["email-to-cal"]
# The portal configures everything from the browser; bind all interfaces inside the
# container and let the published port decide who can reach it.
CMD ["serve", "--host", "0.0.0.0"]
