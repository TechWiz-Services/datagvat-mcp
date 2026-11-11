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
COPY pyproject.toml ./
COPY src/ src/
COPY tests/ tests/
RUN python -m pytest && echo ok > /build/tests-passed

# ---- runtime ----------------------------------------------------------------
FROM base AS runtime
COPY --from=deps /install /usr/local
COPY --from=test /build/tests-passed /usr/local/share/datagvat-mcp/tests-passed
COPY src/datagvat_mcp /app/datagvat_mcp

# No pip in the final image; precompile bytecode (root filesystem may be read-only).
RUN python -m pip uninstall -y -q pip \
 && python -m compileall -q /app \
 && adduser -D -H -u 10001 mcp

ENV PYTHONPATH=/app \
    HOME=/tmp \
    DATAGVAT_HOST=0.0.0.0 \
    DATAGVAT_PORT=8000

WORKDIR /app
USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('DATAGVAT_PORT','8000'), timeout=4)"]

ENTRYPOINT ["python", "-m", "datagvat_mcp"]
