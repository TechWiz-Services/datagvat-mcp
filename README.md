# datagvat-mcp

Self-hosted [MCP](https://modelcontextprotocol.io) server for **Austrian Open Government Data** from [data.gv.at](https://www.data.gv.at) (~76,000 datasets). Agents connect over Streamable HTTP at `http://<host>:8000/mcp`. Put a reverse proxy such as Traefik in front for TLS.

- Small Alpine image (~130 MB, ~55 MB RAM). It runs as a non-root user, works with a read-only root filesystem and has no pip in the final image.
- **Polite to data.gv.at**: one shared rate limit for all agents (1 request/s, burst 3, no parallel connections), `Retry-After` backoff, and an in-memory cache with a 1 h TTL for searches and 24 h for datasets, catalogues and vocabularies.
- **Compact answers**: raw API documents come with labels in ~25 languages and facet lists of up to 500 KB. Tools return trimmed, single-language results instead.
- Self-contained: all code lives in this repository. Dependencies are pinned with sha256 hashes and installed as prebuilt wheels. The test suite runs during `docker build`, so a broken build never produces an image.

## Run

```sh
cp .env.example .env    # set DATAGVAT_AUTH_TOKEN (e.g. openssl rand -hex 32)
docker compose up -d --build
curl http://localhost:8000/health   # -> ok
```

Or without compose:

```sh
docker build -t datagvat-mcp .
docker run -d --name datagvat-mcp -p 8000:8000 --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges:true \
  -e DATAGVAT_AUTH_TOKEN=change-me datagvat-mcp
```

## Connect agents

Claude Code:

```sh
claude mcp add --transport http datagvat https://datagvat-mcp.example.com/mcp \
  --header "Authorization: Bearer change-me"
```

Generic MCP client configuration:

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

Behind Traefik, point the service at port 8000 (see the labels in `docker-compose.yml`). Responses are SSE streams, which Traefik passes through without extra configuration. If your clients connect through a load balancer with several replicas, either use sticky sessions or set `DATAGVAT_STATELESS=true`.

## Tools

| Tool | What it does |
|---|---|
| `search_datasets` | Full-text search with filters: themes, formats, catalogues, High-Value Datasets only, date range. Sorting, paging and optional facet counts |
| `semantic_search_datasets` | Plain-language request. Uses the client's own LLM (MCP sampling) to turn it into German keywords and themes, and falls back to plain search if the client can't |
| `get_dataset` | Full metadata: description, publisher, contacts, licenses, update frequency, coverage, provenance, distributions with URLs |
| `get_dataset_distributions` | Just the files/APIs of a dataset |
| `list_catalogues` | Publisher catalogues: the largest ones, or matches for a name ("Wien" → `l9`) |
| `get_catalogue` | Catalogue details plus its most recently updated datasets |
| `find_related_datasets` | Similar datasets (shared themes, keywords, publisher), scored 0-100 |
| `analyze_dataset_quality` | Metadata quality score 0-100 with passed/failed checks |
| `compare_datasets` | 2-5 datasets side by side, including quality scores |
| `list_vocabularies`, `get_vocabulary`, `search_vocabulary_terms` | Controlled vocabularies (themes, file types, licences, EuroVoc, …) |
| `preview_schema`, `preview_data` | Columns/types and first rows of CSV, JSON or GeoJSON files. Accepts a URL or just a `dataset_id`; downloads at most 512 KB |

Also provided: the resources `datagvat://themes`, `datagvat://datasets/{id}` and `datagvat://catalogues/{id}`, and the prompts `find_datasets`, `quality_audit`, `compare_datasets` and `catalogue_overview`.

Search tips (this is how the data.gv.at search engine behaves): German terms work best. Several words are **OR**-combined and ranked by relevance; write `verkehr AND wien` to require both. Use `"quotes"` for phrases, `europ*` for prefixes and `gesundheit~` for fuzzy matching. `NOT` and `-word` are not supported.

## Configuration

All settings are environment variables.

| Variable | Default | Purpose |
|---|---|---|
| `DATAGVAT_AUTH_TOKEN` | unset | Require `Authorization: Bearer <token>` on every request except `/health`. Without it, anyone who can reach the port can use the server (a warning is logged at startup) |
| `DATAGVAT_ALLOWED_HOSTS` | unset | Comma-separated `Host` header allowlist (DNS-rebinding protection). Use `host:*` to allow any port |
| `DATAGVAT_CONTACT` | unset | E-mail/URL appended to the User-Agent, so data.gv.at can reach you if something goes wrong |
| `DATAGVAT_PORT` / `DATAGVAT_PATH` | `8000` / `/mcp` | Listen port and MCP endpoint path |
| `DATAGVAT_STATELESS` | `false` | Stateless HTTP: no sessions and no sampling |
| `DATAGVAT_LANGUAGE` | `de` | Preferred language for labels (falls back to de, then en) |
| `DATAGVAT_RATE_LIMIT` / `DATAGVAT_RATE_BURST` | `1.0` / `3` | Requests per second to data.gv.at (sustained) and burst size. Preview downloads use the same limit per file host |
| `DATAGVAT_MAX_CONCURRENCY` | `1` | Parallel connections to data.gv.at |
| `DATAGVAT_QUEUE_TIMEOUT` | `60` | Seconds a request may wait for the rate limiter before the tool reports "busy" |
| `DATAGVAT_CACHE_TTL` / `DATAGVAT_CACHE_TTL_STATIC` | `3600` / `86400` | Cache lifetime in seconds for searches / for datasets, catalogues and vocabularies |
| `DATAGVAT_CACHE_MAX_ENTRIES` | `2000` | Cache size (LRU) |
| `DATAGVAT_PREVIEW_MAX_BYTES` | `524288` | Maximum bytes downloaded per preview |
| `DATAGVAT_REQUEST_TIMEOUT` | `30` | Upstream timeout in seconds |
| `DATAGVAT_LOG_LEVEL` | `INFO` | Log level |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxy IPs/CIDRs whose `X-Forwarded-*` headers are trusted. This only affects logged client IPs |

### Why these rate-limit defaults

data.gv.at publishes no limit for its hub API. The Federal Chancellery's RIS open data interface, which refers to the [data.gv.at netiquette](https://www.data.gv.at/infos/netiquette/), asks automated clients for at most 0.5 requests/s, no parallel connections and a contact address in the User-Agent. Those rules are for bulk downloads. An agent answering a question needs a few requests in a row, so the defaults allow a burst of 3 and then 1 request/s, use a single connection and lean heavily on caching: in testing, 21 tool calls cost 16 upstream requests. To be extra careful, set `DATAGVAT_RATE_LIMIT=0.5`.

## Security

- **Read-only.** Every tool only reads public data. There is no write access to data.gv.at and no API key.
- **SSRF protection for previews.** Preview downloads go only to publicly routable IPs. Every redirect hop is checked, the connection is pinned to the validated IP (so DNS rebinding doesn't work), TLS is verified against the hostname, and proxy environment variables are ignored. Downloads are capped at 512 KB.
- **Input validation.** IDs are validated against a strict pattern and URL-encoded.
- **Supply chain.** 29 runtime packages, all pinned by version and sha256 hash, installed with `pip --require-hashes --only-binary=:all:`. Nothing is compiled or fetched from git during the build.

## Development

```sh
docker build --target test -t datagvat-mcp:test .     # runs the offline test suite
docker run --rm -e DATAGVAT_LIVE_TESTS=1 -w /build datagvat-mcp:test python -m pytest   # + live API checks
```

The live tests send a handful of requests to verify the API still looks the way the server expects. They are worth running when data.gv.at changes something.

To update dependencies, run `scripts/update-requirements.sh`, review the diff, then rebuild (tests run during the build).

Layout:

```
src/datagvat_mcp/
  server.py      MCP tools, resources, prompts
  client.py      data.gv.at hub-search client (cache, retries, request coalescing)
  throttle.py    token-bucket rate limiter shared by all sessions
  normalize.py   compact single-language views of API documents
  preview.py     guarded partial downloads + CSV/JSON/GeoJSON parsing
  net_guard.py   SSRF guard transport
  quality.py, similarity.py, semantic.py
  app.py         ASGI app: /health, bearer auth; __main__.py starts uvicorn
tests/           pytest suite (offline; test_live.py opt-in)
```

## Origin

Based on [Julian-AT/datagvat-mcp](https://github.com/Julian-AT/datagvat-mcp) (MIT, as declared in its `pyproject.toml`), reviewed at commit `bf53b23`. The review found no backdoors, telemetry or obfuscated code. The server was then largely rewritten:

- **Framework.** It moved from fastmcp 2.x (~90 dependencies) to the official `mcp` SDK (29).
- **API target.** It now uses data.gv.at's hub-search API correctly. Upstream called endpoints that don't exist there (`/distributions`, `/metrics`, `/record`), listed catalogues as bare IDs, downloaded whole vocabularies (EuroVoc is 6.5 MB) to search them, used a wrong facet name for related datasets and a non-working title sort, misdocumented the query syntax, missed the `OP_DATPRO` theme, mis-parsed sampling results and passed list-typed prompt arguments.
- **Removed tools.** Upstream tools that cannot work against data.gv.at were dropped: DOI eligibility (needs an API key), metrics, catalogue records, resource types and the gazetteer (broken on the server side).
- **Added.** Rate limiting, caching, the SSRF fix, preview fallbacks for dead links, Windows-1252/decimal-comma/GeoJSON support, a local quality score and `compare_datasets` were added.
