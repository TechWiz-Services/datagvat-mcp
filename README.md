# datagvat-mcp (container)

Docker image that serves [Julian-AT/datagvat-mcp](https://github.com/Julian-AT/datagvat-mcp), an MCP server for Austrian Open Government Data (data.gv.at), over **Streamable HTTP**. Agents connect to `http://<host>:8000/mcp`. TLS is left to a reverse proxy such as Traefik.

- Base: `python:3.12-alpine`, multi-stage. The runtime image has no pip, git or compiler.
- Upstream code is fetched at a **pinned, reviewed commit** (`bf53b23`) during the build. The build checks the commit hash.
- Dependencies are installed **exactly as pinned in upstream `uv.lock`, sha256-verified**, as prebuilt wheels only (`scripts/lock2req.py` converts the lockfile).
- The container runs as uid 10001, works with a read-only root filesystem, and has a `/health` endpoint plus a Docker `HEALTHCHECK`.

## Run

```sh
docker compose up -d --build
# or
docker build -t datagvat-mcp .
docker run -d --name datagvat-mcp -p 8000:8000 --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges:true \
  -e MCP_AUTH_TOKEN=change-me datagvat-mcp
```

| Variable | Default | Purpose |
|---|---|---|
| `MCP_PORT` | `8000` | Listen port |
| `MCP_PATH` | `/mcp` | MCP endpoint path |
| `MCP_AUTH_TOKEN` | unset | If set, every request except `/health` must send `Authorization: Bearer <token>` |
| `MCP_STATELESS` | `false` | `true` turns off sessions (no sticky routing needed), but also turns off LLM sampling (see below) |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxy IPs/CIDRs whose `X-Forwarded-*` headers are trusted (only affects logged client IPs) |
| `AUSTRIA_MCP_LOG_LEVEL` | `INFO` | Log level |
| `AUSTRIA_MCP_REQUEST_TIMEOUT` | `30` | Upstream API timeout (s) |

## Connect agents

Claude Code:

```sh
claude mcp add --transport http datagvat https://datagvat-mcp.example.com/mcp \
  --header "Authorization: Bearer change-me"
```

Generic MCP client config:

```json
{
  "mcpServers": {
    "datagvat": {
      "type": "http",
      "url": "https://datagvat-mcp.example.com/mcp",
      "headers": { "Authorization": "Bearer change-me" }
    }
  }
}
```

Behind Traefik, point the service at port 8000 (labels example in `docker-compose.yml`). Responses are SSE streams, which Traefik passes through without extra config.

## Security review of upstream (commit `bf53b23`, 2026-02-04)

All of the server code in `mcp/app/` was reviewed line by line, along with the CLI, the scripts and the Dockerfile, and the tree was scanned for obfuscation, hidden Unicode, `eval`/`exec`/`subprocess`/`pickle`/`base64`, telemetry and unexpected network endpoints.

- **No backdoors, telemetry or obfuscated code.** The server only talks to `www.data.gv.at`, plus whatever URL a client passes to the preview tools. Tool descriptions and prompts contain no hidden instructions. `semantic_search_datasets` asks the *connecting client's* LLM (MCP sampling) to turn the query into keywords. The prompt is plain and the tool falls back to a normal search if the client doesn't support sampling.
- The CLI (`datagvat-mcp init/doctor`) writes MCP config entries into local AI-tool config files and runs only `python --version`. It isn't used in this image.
- The `docs/` folder (Next.js website and chat app) is not part of this image.

**Fixed here** (`patches/0001-preview-ssrf-guard.patch` and `src/net_guard.py`):

1. **SSRF in `preview_schema` / `preview_data`.** Upstream fetches any `http(s)` URL a client supplies, follows redirects, and returns the parsed content. Over HTTP, any connected agent could use that to read internal services: other containers, the LAN, `169.254.169.254`. Outbound preview requests now only go to globally routable IPs. Every redirect hop is checked, the connection is pinned to the validated IP (so DNS rebinding doesn't work), TLS is still verified against the hostname, and proxy environment variables are ignored.
2. **Unbounded download.** When a server ignored the `Range` header, upstream read the whole body into memory. The body is now streamed and reading stops at the 512 KB preview limit.

**Added:** an optional bearer-token check (`MCP_AUTH_TOKEN`). Upstream has no authentication. All tools are read-only and only touch public data, but the token keeps random clients off your instance.

**Not changed (low risk):** dataset/catalogue IDs are put into API paths without encoding. That can only reach other read-only paths on `data.gv.at` itself.

**Dependency note:** versions come from upstream's lockfile (early 2026: fastmcp 2.14.3, mcp 1.25.0, starlette 0.51.0, …). Check them for newer CVEs from time to time, or bump them with upstream.

## Tests

```sh
docker build --target build -t datagvat-mcp:build .
docker run --rm -e PYTHONPATH=/install/lib/python3.12/site-packages \
  -v ./tests:/src/mcp/tests_container:ro datagvat-mcp:build \
  sh -c 'pip install -q pytest==9.0.2 pytest-asyncio==1.3.0 && python -m pytest -q -p no:cacheprovider tests_container'
```

`tests/test_net_guard.py` covers the SSRF guard and the patched fetch. Of upstream's own suite, 45 tests already fail at the pinned commit because they rely on fastmcp internals that changed. The patch only replaces the 7 tests that mocked the old fetch implementation.

## Known upstream issues (not caused by the container)

- `get_dataset_distributions` and `get_dataset_metrics` return "Resource not found": the search API has no such endpoints. `get_dataset` includes the distributions anyway.
- `list_catalogues` ignores `limit`.

## Updating upstream

Change `UPSTREAM_COMMIT` in the `Dockerfile` **only after reviewing the diff** (`git diff bf53b23..<new> -- mcp/`). If the patch no longer applies, the build fails on purpose.

## License

Upstream declares MIT in `mcp/pyproject.toml`. The upstream repository has no `LICENSE` file at this commit.
