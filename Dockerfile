# syntax=docker/dockerfile:1

ARG PYTHON_IMAGE=python:3.12-alpine

# ---- base -----------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

# ---- deps: exact, sha256-verified wheels only (nothing compiled) ----------
FROM base AS deps
COPY requirements.txt /tmp/requirements.txt
RUN pip install --require-hashes --only-binary=:all: --no-deps --prefix=/install -r /tmp/requirements.txt

# ---- test: the image only builds if the test suite passes (no network) ----
FROM base AS test
COPY --from=deps /install /usr/local
COPY requirements-dev.txt /tmp/requirements-dev.txt
RUN pip install --require-hashes --only-binary=:all: --no-deps -r /tmp/requirements-dev.txt
WORKDIR /build
# The docs and example files are checked against the code by tests/test_docs.py.
COPY pyproject.toml README.md .env.example docker-compose.yml Dockerfile ./
COPY src/ src/
COPY tests/ tests/
RUN python -m pytest && echo ok > /build/tests-passed

# ---- runtime ----------------------------------------------------------------
FROM base AS runtime
# Links the package on ghcr.io to the repository. Version and revision labels are added at build time.
LABEL org.opencontainers.image.title="datagvat-mcp" \
      org.opencontainers.image.description="Self-hosted MCP server for Austrian Open Government Data (data.gv.at)" \
      org.opencontainers.image.source="https://github.com/TechWiz-Services/datagvat-mcp" \
      org.opencontainers.image.url="https://github.com/TechWiz-Services/datagvat-mcp" \
      org.opencontainers.image.licenses="MIT"
COPY --from=deps /install /usr/local
COPY --from=test /build/tests-passed /usr/local/share/datagvat-mcp/tests-passed
COPY src/datagvat_mcp /app/datagvat_mcp

# No pip in the final image; precompile bytecode (root filesystem may be read-only).
RUN python -m pip uninstall -y -q pip \
 && python -m compileall -q /app \
 && adduser -D -H -u 10001 mcp \
 && mkdir /cache && chown 10001:10001 /cache

ENV PYTHONPATH=/app \
    HOME=/tmp \
    DATAGVAT_HOST=0.0.0.0 \
    DATAGVAT_PORT=8000 \
    DATAGVAT_CACHE_DIR=/cache

# Saved answers from data.gv.at. Mount a named volume here to keep them when the
# container is replaced; without one the server falls back to caching in memory.
VOLUME /cache

WORKDIR /app
USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('DATAGVAT_PORT','8000'), timeout=4)"]

ENTRYPOINT ["python", "-m", "datagvat_mcp"]
