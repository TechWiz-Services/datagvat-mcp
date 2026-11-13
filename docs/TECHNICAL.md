# Technical reference

For operators and contributors. The [README](../README.md) is the quick start.

## Components

| File (`src/datagvat_mcp/`) | Role |
|---|---|
| `__main__.py` | Entry point. Refuses to start without token and contact, configures logging, runs uvicorn |
| `app.py` | ASGI app: `/health`, bearer-token middleware, MCP mount, lifespan (closes services) |
| `server.py` | Tools, resources, prompts; wiring of client, preview fetcher and throttles; `cache` note in results |
| `client.py` | hub-search client: retries, request coalescing, stale fallback |
| `cache.py` | SQLite answer cache, one limit and one lifetime per category |
| `throttle.py` | One request in flight, pause after each; per-host variant |
| `preview.py` | Partial downloads, format detection, CSV/JSON/GeoJSON parsing |
| `net_guard.py` | HTTP transport that only connects to public addresses |
| `normalize.py` | Compact single-language views of API documents, tolerant accessors |
| `quality.py`, `similarity.py`, `semantic.py` | Local quality score, related datasets, query expansion via MCP sampling |
| `config.py` | Settings (environment, validation, startup checks) |

## Request path

1. Tool call arrives; pydantic validates arguments (ranges, patterns); ids must match `^[A-Za-z0-9][A-Za-z0-9._:~@+-]{0,199}$`.
2. `client.get(path, params, category, refresh)` builds the key `path?sorted-params` and looks it up in the cache. A fresh hit is returned (unless `refresh` and the copy is at least 300 s old).
3. Identical requests in flight share one upstream task. A cancelled caller does not cancel the task.
4. The task waits for the throttle, sends the request, retries per the table below, stores a plausible answer.
5. On `ApiError` (not on 404) with a saved copy available, the saved copy is returned, fresh or stale.
6. The tool normalises the document. If any part came from the cache, the result gets a `cache` entry.

## Settings

Environment variables. Defaults are tested against the code (`tests/test_docs.py`).

### Access

| Variable | Default | Range / values | Meaning |
|---|---|---|---|
| `DATAGVAT_AUTH_TOKEN` | required | non-blank | Bearer token for every request except `GET /health` |
| `DATAGVAT_CONTACT` | required | non-blank | Appended to the User-Agent |
| `DATAGVAT_ALLOWED_HOSTS` | unset | comma-separated | `Host` allowlist; also restricts `Origin` to `http(s)://<host>`. `host:*` allows any port |
| `DATAGVAT_HOST` | `0.0.0.0` | | Listen address |
| `DATAGVAT_PORT` | `8000` | 1–65535 | Listen port |
| `DATAGVAT_PATH` | `/mcp` | leading `/` added | MCP endpoint |
| `DATAGVAT_STATELESS` | `false` | bool | No sessions, so no sampling |
| `DATAGVAT_LOG_LEVEL` | `INFO` | DEBUG, INFO, WARNING, ERROR, CRITICAL | Other values stop the start |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | IPs/CIDRs | uvicorn: proxies whose `X-Forwarded-*` is trusted (logged IPs only) |

### Upstream and throttling

| Variable | Default | Range / values | Meaning |
|---|---|---|---|
| `DATAGVAT_API_BASE` | `https://www.data.gv.at/api/hub/search` | URL | hub-search base URL |
| `DATAGVAT_LANGUAGE` | `de` | any code | Label language; falls back to de, then en |
| `DATAGVAT_REQUEST_TIMEOUT` | `30` | 5–120 | Seconds per upstream request |
| `DATAGVAT_REQUEST_INTERVAL` | `2` | 0.5–60 | Pause in seconds after each completed API request. Below 0.5 stops the start |
| `DATAGVAT_PREVIEW_REQUEST_INTERVAL` | `2` | 0.5–60 | Same, per file host (previews). Independent of the API pause |
| `DATAGVAT_QUEUE_TIMEOUT` | `30` | 1–600 | Seconds a request may wait for its turn |

### Previews

| Variable | Default | Range / values | Meaning |
|---|---|---|---|
| `DATAGVAT_PREVIEW_MAX_BYTES` | `524288` | 4096–8388608 | Bytes downloaded per preview |
| `DATAGVAT_PREVIEW_BLOCKED_HOSTS` | `ris.bka.gv.at,data.bka.gv.at` | comma-separated | Hosts, subdomains included, the preview tools refuse; also checked on every redirect hop |

### Cache

TTL in seconds, size in MB. `0` switches a category off.

| Variable | Default | Range / values | Meaning |
|---|---|---|---|
| `DATAGVAT_CACHE_ENABLED` | `true` | bool | Master switch: nothing stored, no saved copy served, no file written |
| `DATAGVAT_CACHE_DIR` | unset (`/cache` in the image) | path | SQLite file location. Unset or unusable: in memory, 16 MB per category |
| `DATAGVAT_CACHE_SEARCH_TTL` | `21600` | ≥0 | Dataset searches |
| `DATAGVAT_CACHE_SEARCH_MAX_MB` | `64` | 0–102400 | |
| `DATAGVAT_CACHE_DATASET_TTL` | `604800` | ≥0 | `/datasets/{id}` |
| `DATAGVAT_CACHE_DATASET_MAX_MB` | `128` | 0–102400 | |
| `DATAGVAT_CACHE_CATALOGUE_TTL` | `604800` | ≥0 | `/catalogues/{id}`, catalogue searches, catalogue facet list |
| `DATAGVAT_CACHE_CATALOGUE_MAX_MB` | `16` | 0–102400 | |
| `DATAGVAT_CACHE_VOCABULARY_TTL` | `2592000` | ≥0 | `/vocabularies` and vocabulary term searches |
| `DATAGVAT_CACHE_VOCABULARY_MAX_MB` | `32` | 0–102400 | |
| `DATAGVAT_CACHE_PREVIEW_TTL` | `3600` | ≥0 | First bytes of data files, keyed by URL |
| `DATAGVAT_CACHE_PREVIEW_MAX_MB` | `64` | 0–102400 | |
| `DATAGVAT_CACHE_MAX_STALE` | `2592000` | ≥0 | Seconds past TTL a copy is kept as fallback. `0` disables the fallback |

## Throttle and retries

- `Throttle(interval, queue_timeout)`: one `asyncio.Lock`, FIFO. A request holds it while in flight; when it ends (success or failure) the next start is held back by `interval`. The pause counts from completion.
- `queue_timeout` bounds the wait for the lock plus the wait for the pause. Exceeding it raises `ThrottleTimeout`, shown to the agent as "queue is full".
- `penalize(s)` pushes the next start out by at least `s` (after 429/503).
- `Settings` enforces the 0.5 s floor on both intervals. The `Throttle` class itself accepts 0 so tests can run without waiting.
- `HostThrottles`: one throttle per host (max 256, oldest evicted). The API host, with and without `www.`, is registered to the API throttle, so files hosted on data.gv.at share its queue.

| Upstream result | Behaviour |
|---|---|
| 404, or 200 with empty `result` | `NotFoundError`, not cached, not retried |
| 429, 502, 503, 504 | Wait `Retry-After` (seconds or HTTP date) or `2^attempt` s, up to 3 attempts. 429/503 also penalise the throttle. A wait above 30 s gives up at once: "busy" |
| Timeout, transport error | Up to 3 attempts, sleeping 2 s, then 4 s |
| Other 4xx/5xx | `ApiError` with the first 200 characters of the body, not retried |
| 200, not JSON | `ApiError` |

Search window: `page * limit` must stay below 10000.

## Cache

- **Store.** `cache.sqlite3` in `DATAGVAT_CACHE_DIR`; WAL; table `entries(category, key, value, size, stored_at, expires_at, last_used)`; value is zlib-compressed JSON; size counts compressed value plus key. Schema version in `PRAGMA user_version`; a different version drops the table. A damaged file is deleted and recreated; an unusable directory falls back to an in-memory database.
- **Eviction.** After each write, the category's least recently used entries are deleted until it fits its limit. An entry larger than the limit is not stored. At start, entries older than TTL + max stale are purged and limits re-applied.
- **Stored only if plausible.** A non-empty `result` object, or a non-empty vocabulary list. Failures are never stored.
- **Stale.** Past TTL an entry is kept for `CACHE_MAX_STALE` and served only when the source fails with an `ApiError` (previews: timeout, connection error, 429, 5xx).
- **Refresh.** `refresh: true` on `search_datasets`, `get_dataset`, `preview_data`, `preview_schema` bypasses a fresh copy if it is at least 300 s old.
- **Result note.** `cache: {"saved_copy_from": "<UTC ISO time>", "stale": bool, "warning"?}` is added to dict results when any part came from the cache. Tracked per call with a context variable.
- **Previews.** Stored under category `preview` by URL; content is base64 in the JSON value.

## Previews

- **Download.** `Range: bytes=0-(max-1)`, `Accept-Encoding: identity`, redirects up to 5, `trust_env=False`. Status 200/206 accepted; the body is read up to `max + 1` bytes and cut. Dataset targets try up to 3 distributions, previewable formats first.
- **Detection.** HTML under any URL is refused. Otherwise content type, then URL extension, then first character (`{`/`[` JSON) or a delimiter in the first line (CSV).
- **Binary refusal.** ZIP/Office, PDF, gzip, legacy Office, or NUL bytes in the first 4 KB (UTF-16 with BOM excepted).
- **Decoding.** UTF-8 (BOM tolerated, cut-off final character tolerated when partial), UTF-16 with BOM, else Windows-1252.
- **CSV.** Delimiter from `, ; tab |` via sniffing; types from the first 50 rows; at most 100 rows returned; cells cut at 300 characters; field size limit 16 MB; a cut-off last line is dropped when partial; malformed rows are counted, not returned.
- **JSON.** Truncated arrays are recovered by closing brackets after the last complete object. GeoJSON features become rows of properties plus `geometry_type`.
- **Block list.** Host checked before the cache lookup and again on every redirect hop.
- **Network guard.** `PublicOnlyTransport` resolves the name and refuses unless every address is public (`is_global`, not multicast, not site-local). IPv4-mapped, NAT64 and IPv4-compatible IPv6 addresses are judged by the embedded IPv4 address. The connection goes to the validated address (IPv4 first); `Host` and TLS SNI keep the original name. Only `http`/`https`.

## Security

- **Token.** `BearerTokenMiddleware` compares with `hmac.compare_digest`. Only the exact path `/health` and lifespan events skip it; WebSocket connections without a valid token are closed with 1008.
- **Startup.** `Settings.startup_problems()` lists missing token or contact; `__main__` exits with status 1 and the reasons on stderr. The token is never printed.
- **Host allowlist.** Sets MCP transport security (DNS-rebinding protection) with the given hosts and `http(s)` origins.
- **Upstream data.** Documents are treated as untrusted: accessors in `normalize.py` never assume a type; `tests/test_robustness.py` replaces every field with wrong-typed values.
- **Container.** Alpine, uid 10001, no pip, read-only root filesystem compatible, `/cache` volume, healthcheck on `/health`.

## Errors

`tool_errors` turns `NotFoundError`, `ApiError`, `PreviewError` and `ValueError` into MCP tool errors (`isError`) with a readable message. Other exceptions propagate as protocol errors. Sampling (`semantic_search_datasets`) times out after 25 s and falls back to a plain search.

## Tests

| File | Covers |
|---|---|
| `test_throttle.py`, `test_client.py`, `test_cache.py` | Spacing, retries, coalescing, cancellation, stale fallback, eviction, damaged files |
| `test_server.py`, `test_app.py`, `test_main.py` | Tools end to end over MCP, HTTP sessions and auth, startup refusal |
| `test_preview.py`, `test_net_guard.py` | Download limits, parsing, block list, address checks |
| `test_normalize.py`, `test_robustness.py`, `test_quality.py`, `test_similarity.py`, `test_semantic.py` | Views and scoring, wrong-typed upstream data |
| `test_config.py`, `test_docs.py` | Validation, and that README, this file, compose and `.env.example` match the code |
| `test_live.py` | Opt-in (`DATAGVAT_LIVE_TESTS=1`): real API shape |

- Offline suite: the `test` stage of the Dockerfile (a failing test means no image). For a quick loop, mount the working tree into a test-stage image and run `python -m pytest`.
- Fixtures: `FakeApi` (mock transport), `FakeClock`, `instant_throttle()` (no pause; `Settings` itself refuses intervals below 0.5 s).

## Release

Version in `pyproject.toml` and `src/datagvat_mcp/__init__.py` (a test keeps them equal). Push a tag `v<version>`: `.github/workflows/publish.yml` builds `linux/amd64` and `linux/arm64`, runs the tests inside the build and pushes `<version>`, `<major.minor>` and `latest` to `ghcr.io`. `ci.yml` builds the test stage on every push and pull request. Actions are pinned by commit hash. Dependencies: `scripts/update-requirements.sh`, then review the diff.
