# The Drella API. Build from the repo root:
#   docker build -f images/drella/Dockerfile -t drella .
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

COPY --from=ghcr.io/astral-sh/uv:0.8.22@sha256:9874eb7afe5ca16c363fe80b294fe700e460df29a55532bbfea234a0f12eddb1 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY drella ./drella
RUN uv sync --frozen --no-dev

# The image's version: a release tag like v0.1.0, or sha-<commit> for builds from
# main (CI sets it; a local build gets "main"). Drella launches the drella-job image
# with the same tag.
ARG DRELLA_VERSION=main
ENV DRELLA_VERSION=$DRELLA_VERSION

RUN useradd --uid 1000 --create-home drella && mkdir -p /data && chown drella /data
USER 1000
ENV PATH=/app/.venv/bin:$PATH DRELLA_STORAGE_PATH=/data PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["drella", "serve"]
