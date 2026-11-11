# syntax=docker/dockerfile:1

# ---- build: fetch the reviewed upstream commit, patch it, install locked deps
FROM python:3.12-alpine AS build

# Upstream source, pinned to the exact commit that was security-reviewed.
ARG UPSTREAM_REPO=https://github.com/Julian-AT/datagvat-mcp.git
ARG UPSTREAM_COMMIT=bf53b236a5f4a82391310226a4c82103501661f8

RUN apk add --no-cache git

WORKDIR /src
RUN git init -q . \
 && git remote add origin "$UPSTREAM_REPO" \
 && git fetch -q --depth 1 origin "$UPSTREAM_COMMIT" \
 && git checkout -q FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "$UPSTREAM_COMMIT"

WORKDIR /src/mcp
COPY src/net_guard.py app/net_guard.py
COPY patches/ /tmp/patches/
RUN git apply /tmp/patches/*.patch

# Runtime dependencies exactly as pinned in upstream uv.lock, sha256-verified,
# prebuilt wheels only (nothing is compiled from source).
COPY scripts/lock2req.py /tmp/lock2req.py
RUN python /tmp/lock2req.py uv.lock > /tmp/requirements.txt \
 && pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore \
      --require-hashes --only-binary=:all: --no-deps --prefix=/install \
      -r /tmp/requirements.txt

# ---- runtime
FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    AUSTRIA_MCP_LOG_LEVEL=INFO \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    MCP_PATH=/mcp

COPY --from=build /install /usr/local
COPY --from=build /src/mcp/app /srv/app
COPY src/serve.py /srv/serve.py

# Drop pip from the final image; precompile app bytecode (rootfs may be read-only).
RUN python -m pip uninstall -y -q --root-user-action=ignore pip \
 && python -m compileall -q /srv \
 && adduser -D -H -u 10001 mcp

WORKDIR /srv
USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('MCP_PORT','8000'), timeout=4)"]

ENTRYPOINT ["python", "serve.py"]
