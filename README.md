# datagvat-mcp

Self-hosted [MCP](https://modelcontextprotocol.io) server for **Austrian Open Government Data** from [data.gv.at](https://www.data.gv.at) (~76,000 datasets). Agents connect over Streamable HTTP at `http://<host>:8000/mcp`. Put a reverse proxy such as Traefik in front for TLS.

- Small Alpine image (~130 MB, ~55 MB RAM). It runs as a non-root user, works with a read-only root filesystem and has no pip in the final image.
- **Polite to data.gv.at**: all agents together send one request at a time, with a 2 s pause after each answer. The server obeys `Retry-After` and keeps answers in a cache on disk for hours to weeks, so most repeated questions cost data.gv.at nothing. See [The data.gv.at API and its etiquette](#the-datagvat-api-and-its-etiquette).
- **Compact answers**: raw API documents come with labels in ~25 languages and facet lists of up to 500 KB. Tools return trimmed, single-language results instead.
- Self-contained: all code lives in this repository. Dependencies are pinned with sha256 hashes and installed as prebuilt wheels. The test suite runs during `docker build`, so a broken build never produces an image.

This is an unofficial project. It is not affiliated with or endorsed by data.gv.at or the Austrian federal administration.

## Run

Images for `linux/amd64` and `linux/arm64` are published to the GitHub Container Registry as `ghcr.io/techwiz-services/datagvat-mcp`. Tags: `latest`, the exact version (`2.0.0`) and the minor version (`2.0`). For a pinned setup use a version tag instead of `latest`, or the image digest.

Example `docker-compose.yml`. Only the two settings you should set yourself are listed; everything else keeps its default (see [Configuration](#configuration)):

```yaml
services:
  datagvat-mcp:
    image: ghcr.io/techwiz-services/datagvat-mcp:latest
    restart: unless-stopped
    ports:
      - "8000:8000"   # remove when only a reverse proxy should reach the container
    environment:
      # Clients must send "Authorization: Bearer <token>"
      DATAGVAT_AUTH_TOKEN: ${DATAGVAT_AUTH_TOKEN:?set a token in .env}
      # Sent to data.gv.at in the User-Agent so they can reach you
      DATAGVAT_CONTACT: ${DATAGVAT_CONTACT:-}
    volumes:
      - datagvat-cache:/cache   # keeps saved answers across updates
    read_only: true
    tmpfs:
      - /tmp
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    mem_limit: 256m

volumes:
  datagvat-cache:
```

Next to it, a `.env` file:

```sh
DATAGVAT_AUTH_TOKEN=      # generate one with: openssl rand -hex 32
DATAGVAT_CONTACT=         # your e-mail address or a URL
```

Then `docker compose up -d` and check `curl http://localhost:8000/health` (answers `ok`).

## Connect agents

MCP client configuration:

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

Behind Traefik, add the usual labels and point the service at port 8000. Responses are SSE streams, which Traefik passes through without extra configuration. If your clients connect through a load balancer with several replicas, either use sticky sessions or set `DATAGVAT_STATELESS=true`.

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

All settings are environment variables. With docker compose, add the ones you want to change under `environment:`.

| Variable | Default | Purpose |
|---|---|---|
| `DATAGVAT_AUTH_TOKEN` | unset | Require `Authorization: Bearer <token>` on every request except `/health`. Without it, anyone who can reach the port can use the server (a warning is logged at startup) |
| `DATAGVAT_ALLOWED_HOSTS` | unset | Comma-separated `Host` header allowlist (DNS-rebinding protection). Use `host:*` to allow any port |
| `DATAGVAT_CONTACT` | unset | E-mail address or URL appended to the User-Agent, so data.gv.at can reach you if something goes wrong. Please set it |
| `DATAGVAT_HOST` / `DATAGVAT_PORT` / `DATAGVAT_PATH` | `0.0.0.0` / `8000` / `/mcp` | Listen address, port and MCP endpoint path |
| `DATAGVAT_STATELESS` | `false` | Stateless HTTP: no sessions and no sampling |
| `DATAGVAT_LANGUAGE` | `de` | Preferred language for labels (falls back to de, then en) |
| `DATAGVAT_REQUEST_INTERVAL` | `2` | Seconds to pause after each answer from data.gv.at before the next request is sent. Requests are never sent in parallel |
| `DATAGVAT_PREVIEW_REQUEST_INTERVAL` | `2` | The same pause for preview downloads, counted per file host. It is separate so that a faster API setting does not speed up downloads from publishers' servers, which are often small |
| `DATAGVAT_QUEUE_TIMEOUT` | `60` | Seconds a request may wait for its turn before the tool reports "busy" |
| `DATAGVAT_CACHE_ENABLED` | `true` | Master switch for the cache. `false`: nothing is saved and no saved copy is ever served, so every tool call goes to data.gv.at. Leave it on unless you are debugging |
| `DATAGVAT_CACHE_DIR` | `/cache` in the image, otherwise unset | Directory for the cache file. Unset or not writable: the cache lives in memory, is lost on restart and holds at most 16 MB per kind |
| `DATAGVAT_CACHE_SEARCH_TTL` / `DATAGVAT_CACHE_SEARCH_MAX_MB` | `21600` (6 h) / `64` | Dataset searches: lifetime in seconds and size limit in MB |
| `DATAGVAT_CACHE_DATASET_TTL` / `DATAGVAT_CACHE_DATASET_MAX_MB` | `604800` (7 d) / `128` | Metadata of single datasets |
| `DATAGVAT_CACHE_CATALOGUE_TTL` / `DATAGVAT_CACHE_CATALOGUE_MAX_MB` | `604800` (7 d) / `16` | Catalogue details and catalogue lists |
| `DATAGVAT_CACHE_VOCABULARY_TTL` / `DATAGVAT_CACHE_VOCABULARY_MAX_MB` | `2592000` (30 d) / `32` | Vocabularies and their terms |
| `DATAGVAT_CACHE_PREVIEW_TTL` / `DATAGVAT_CACHE_PREVIEW_MAX_MB` | `3600` (1 h) / `64` | Downloaded beginnings of data files |
| `DATAGVAT_CACHE_MAX_STALE` | `2592000` (30 d) | How long past its lifetime an answer is kept as a fallback for when the source cannot be reached. `0` turns the fallback off |
| `DATAGVAT_PREVIEW_MAX_BYTES` | `524288` | Maximum bytes downloaded per preview |
| `DATAGVAT_PREVIEW_BLOCKED_HOSTS` | `ris.bka.gv.at,data.bka.gv.at` | Hosts (including subdomains) the preview tools never download from. The default blocks the legal information system RIS; see [below](#ris-is-blocked-by-default). Set it to an empty value to allow everything |
| `DATAGVAT_REQUEST_TIMEOUT` | `30` | Upstream timeout in seconds |
| `DATAGVAT_API_BASE` | `https://www.data.gv.at/api/hub/search` | Address of the hub-search API. Only change it for testing |
| `DATAGVAT_LOG_LEVEL` | `INFO` | Log level |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxy IPs/CIDRs whose `X-Forwarded-*` headers are trusted. This only affects logged client IPs |

## The data.gv.at API and its etiquette

### Which API this server uses

data.gv.at runs on the open-source [piveau](https://www.piveau.eu) platform. This server talks to one of its services, **hub-search** (version 5.1.12 at the time of writing), at `https://www.data.gv.at/api/hub/search`. Interactive documentation is at [data.gv.at/api/hub/search](https://www.data.gv.at/api/hub/search/), and the machine-readable description is [openapi.yaml](https://www.data.gv.at/api/hub/search/openapi.yaml).

- Reading needs no API key and no registration. The server only reads.
- It uses four endpoints: `/search` (datasets, catalogues and vocabulary terms), `/datasets/{id}`, `/catalogues/{id}` and `/vocabularies`.
- The API returns metadata only. The data files themselves live on the publishers' own servers (cities, ministries, Statistik Austria and many others). The preview tools fetch the first 512 KB of such a file from that server.

### What data.gv.at asks of its users

The [data.gv.at netiquette](https://www.data.gv.at/infos/netiquette/) (German, last changed in May 2024) is the portal's terms of use. In short:

- **Licences apply.** Open data is free to use as long as you keep to its licence. Since March 2019 the default is [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.de): you may copy, publish, adapt and use the data commercially, but you must name the rights holder. Each dataset states its own licence, and a few differ.
- **Tell them about your application.** Providers of services built on the data are asked to report them with the "Anwendung einreichen" form on data.gv.at. The Federal Chancellery may list and report on such applications.
- **No guarantees.** The interface is unavailable during maintenance or technical problems. Data, links and applications come without warranty or liability, also regarding how current or correct they are.
- **Lawful use only.** The data must not be used for applications or publications that break the law or are offensive.
- Austrian law applies.

The netiquette sets **no technical limits**, and none are published for the hub-search API. The only concrete numbers come from the Federal Chancellery's legal information system RIS, a separate service whose [open data page](https://www.ris.bka.gv.at/UI/Ogd.aspx) points to the same netiquette. For anything beyond single lookups it asks for:

- at most 0.5 requests per second, "that is, a pause of two seconds between requests",
- no parallel connections,
- bulk queries only outside office hours (20:00 to 05:00, weekends, Austrian public holidays), announced beforehand by e-mail,
- a User-Agent header with the name of the application and a contact address,
- and it warns that clients which ignore this or disturb the servers can have their IP address blocked.

In August 2026 Austrian media reported that RIS was slowed down by automated requests from AI-driven bots ([ORF](https://orf.at/stories/3439709/)). An MCP server is exactly that kind of client, so this one is deliberately conservative.

### How this server behaves

- **One request at a time, then a pause.** All connected agents share one queue. After each answer from data.gv.at the server waits `DATAGVAT_REQUEST_INTERVAL` (2 s) before sending the next request. The pause starts when the answer has arrived, so slow answers are never followed more quickly. A tool call that needs several documents therefore takes a few seconds.
- **It backs off.** On HTTP 429 or 503 it waits as long as `Retry-After` says (and gives up instead of waiting more than 30 s). Every other queued request waits too.
- **It asks once.** Answers are cached for a long time (see [Caching](#caching)), identical requests made at the same time share one upstream call, and tools request only the fields they need.
- **It identifies itself.** The User-Agent is `datagvat-mcp/<version> (self-hosted MCP server; httpx)`, followed by your `DATAGVAT_CONTACT`.
- **Publishers' file servers get the same treatment.** Previews are fetched one at a time per host with a pause of their own (`DATAGVAT_PREVIEW_REQUEST_INTERVAL`, also 2 s), with a `Range` request for the first 512 KB only, and are cached. Files hosted on data.gv.at itself count against the API's queue.
- **It tells agents the rules.** The server instructions ask agents to prefer few, specific calls, not to page through large result sets, and to name publisher and licence when they pass data on.

### Caching

Public servers in Austria have already been slowed down by AI clients: in August 2026 the Federal Chancellery blamed "automated requests by AI-driven bots" for outages of RIS ([ORF report](https://orf.at/stories/3439709/)). The best request is the one that is never sent, so this server keeps what it has fetched for as long as that is reasonable:

| Kind of answer | Kept for | Why |
|---|---|---|
| Vocabularies, EuroVoc included | 30 days | Change a few times a year at most. Each page or search is cached on its own; no vocabulary is downloaded whole |
| Dataset metadata, catalogues | 7 days | Descriptions and links rarely change |
| Dataset searches | 6 hours | The portal takes in new and changed datasets every day |
| Beginnings of data files (previews) | 1 hour | Some files are updated daily or more often |

- **On disk.** The cache is one SQLite file in `/cache`, so it survives restarts. Mount a named volume there (the compose file does) to keep it across updates as well.
- **Bounded.** Every kind has its own lifetime and its own size limit. When a kind is full, the entries that have not been used for the longest time are dropped. Setting a lifetime or a size to `0` switches that kind off.
- **A saved copy beats an error.** If data.gv.at or a file server is down, overloaded or too slow, the server answers from a saved copy even when it is past its lifetime (up to `DATAGVAT_CACHE_MAX_STALE` longer) instead of trying again and again.
- **Agents are told.** An answer that comes from the cache carries a `cache` entry with the time the copy was fetched, and a warning if it is past its lifetime.
- **Refresh on request.** `search_datasets`, `get_dataset` and the preview tools take `refresh: true` for a new copy. It is ignored while the saved copy is less than 5 minutes old.
- Answers that do not look right (empty or malformed) are never saved.

### RIS is blocked by default

The server never calls the RIS interface itself. But data.gv.at lists RIS datasets, and the preview tools could otherwise download from the RIS servers on an agent's request. By default they refuse (`ris.bka.gv.at` and `data.bka.gv.at`, also as a redirect target), for two reasons:

- RIS has the strict access rules quoted above, and it is the service that was [disrupted by AI bots](https://orf.at/stories/3439709/) in 2026. This server should not add to that.
- This server is meant for everyday open data. Legal texts fetched by an agent invite answers that look like legal advice.

RIS datasets still show up in search results with their metadata and links. If you know what you are doing and keep to the RIS rules, set `DATAGVAT_PREVIEW_BLOCKED_HOSTS=` (empty) or list other hosts there.

### What you should do when you run it

- **Set `DATAGVAT_CONTACT`** to an address where you can be reached. Without it, data.gv.at sees an anonymous client and can only block it.
- **Keep the default interval**, or raise it. `DATAGVAT_REQUEST_INTERVAL=0` removes the pause and is meant for tests against your own mock, not for data.gv.at.
- **Do not use it for bulk harvesting.** It is built for agents that answer questions. If you need large parts of the catalogue, ask data.gv.at how they want that done.
- **Set `DATAGVAT_AUTH_TOKEN`** if anyone else can reach the port. Requests made through your server come from your IP address and are your responsibility.
- **Attribute the data** in whatever you build or publish with it, as the dataset's licence requires.
- If you offer a public service on top of it, **register it** with data.gv.at as the netiquette asks, and choose a name that cannot be mistaken for an official one.

## Security

- **Read-only.** Every tool only reads public data. There is no write access to data.gv.at and no API key.
- **SSRF protection for previews.** Preview downloads go only to publicly routable IPs. A host name is refused if any of its addresses is private, and IPv6 addresses that embed an IPv4 address are judged by that address. Every redirect hop is checked, the connection is pinned to the validated IP (so DNS rebinding doesn't work), TLS is verified against the hostname, and proxy environment variables are ignored. Downloads are capped at 512 KB.
- **Input validation.** IDs are validated against a strict pattern and URL-encoded. Answers from data.gv.at are not trusted to have the documented shape.
- **Supply chain.** 29 runtime packages, all pinned by version and sha256 hash, installed with `pip --require-hashes --only-binary=:all:`. Nothing is compiled or fetched from git during the build.

## Development

The offline test suite runs inside the Docker build, so a failing test means no image. `tests/test_live.py` is skipped unless `DATAGVAT_LIVE_TESTS=1` is set; it sends a handful of requests (spaced like every other request) to verify that the API still looks the way the server expects. It is worth running when data.gv.at changes something. To update dependencies, run `scripts/update-requirements.sh` and review the diff.

Releases: set the new version in `pyproject.toml` and `src/datagvat_mcp/__init__.py` (a test checks that they match), commit, then push a tag `v<version>`. The workflow in `.github/workflows/publish.yml` builds the image for both architectures, runs the test suite inside the build and publishes it. Every push and pull request also runs the test suite (`.github/workflows/ci.yml`). The GitHub Actions used are pinned to commit hashes, like the Python dependencies.

Layout:

```
src/datagvat_mcp/
  server.py      MCP tools, resources, prompts
  client.py      data.gv.at hub-search client (retries, request coalescing, stale fallback)
  cache.py       SQLite answer cache with a lifetime and size limit per kind of answer
  throttle.py    one request at a time with a pause after each, shared by all sessions
  normalize.py   compact single-language views of API documents
  preview.py     guarded partial downloads + CSV/JSON/GeoJSON parsing
  net_guard.py   SSRF guard transport
  quality.py, similarity.py, semantic.py
  app.py         ASGI app: /health, bearer auth; __main__.py starts uvicorn
tests/           pytest suite (offline; test_live.py opt-in)
```

## Contributing

Contributions are welcome: issues, ideas and pull requests. A pull request must keep the test suite passing, and it has to respect these core requirements:

1. **No harm to data.gv.at.** Running this service without modification must never cause any issues for data.gv.at.
2. **Their rules are the ceiling.** The data.gv.at netiquette, and any rules or regulations that may be released for this API in the future, must be observed as absolute maximums.
3. **Nothing related to RIS.** Do not add anything that touches the legal information system RIS.
4. **Security is never reduced.** Multi-user support may be added, but authentication must not be removed.

## License

The code is under the [MIT license](LICENSE).

**The data is not.** This server only gives access to data published by third parties. The MIT license covers this software, not what it fetches. Each dataset has its own license, shown in the `licenses` field of `get_dataset` and in every distribution. Most Austrian open data is [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.de), which requires naming the rights holder, but some datasets use other licenses or terms (for example CC BY-SA, CC0 or the older CC BY 3.0 AT). Metadata from data.gv.at and the files on the publishers' servers can differ in this. If you use or republish data you obtained through this server, check and follow the license of that dataset. The server's instructions ask agents to name publisher and license when they pass data on, but that is no substitute for checking.

## Origin

Based on [Julian-AT/datagvat-mcp](https://github.com/Julian-AT/datagvat-mcp) by Julian S. (MIT), starting from commit `bf53b23`. The server was then rewritten for data.gv.at specifically. What changed:

- **Framework.** It runs on the official `mcp` SDK instead of fastmcp 2.x, with 29 pinned runtime packages instead of about 90.
- **Hub-search API.** The client talks only to the endpoints data.gv.at offers: `/search`, `/datasets/{id}`, `/catalogues/{id}` and `/vocabularies`. Distributions come from the dataset document, and catalogue listings carry titles and dataset counts. Related datasets are found with a keyword query inside the theme facets, titles sort on `title.<language>`, the `OP_DATPRO` theme is known, and the search syntax is documented the way the engine behaves (OR by default, `AND`, phrases, prefixes, fuzzy matching).
- **Vocabularies.** No vocabulary gets special treatment, EuroVoc (7,339 terms, about 6.5 MB) included: terms are looked up on demand, one page or one search at a time, and kept in the regular cache like every other answer.
- **Tool set.** Features that need an API key or an endpoint data.gv.at does not provide (DOI eligibility, metrics, catalogue records, resource types, the gazetteer) are replaced by `compare_datasets` and a local metadata quality score.
- **Compact answers.** Documents are reduced to single-language views, so an agent does not receive labels in 24 languages.
- **Operation.** Request spacing with one request at a time, a disk cache with fallback to saved copies, blocking of requests to internal addresses in previews, fallbacks for dead links, and support for Windows-1252, UTF-16, decimal commas and GeoJSON.
