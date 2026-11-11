"""Client for the data.gv.at hub-search API (piveau), with throttling and caching.

Only hub-search is used: it serves dataset search, dataset/catalogue details
(including distributions) and controlled vocabularies. API documentation:
https://www.data.gv.at/api/hub/search/ (OpenAPI: .../openapi.yaml)
"""

import asyncio
import email.utils
import json
import logging
import re
import time
from typing import Any
from urllib.parse import quote

import httpx

from datagvat_mcp import __version__
from datagvat_mcp.cache import MISSING, TTLCache
from datagvat_mcp.config import Settings
from datagvat_mcp.throttle import Throttle, ThrottleTimeout

logger = logging.getLogger(__name__)

# Identifiers seen on data.gv.at: UUIDs, slugs like "l9" or "gga-31842",
# vocabulary ids like "data-theme", "OP_DATPRO".
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:~@+-]{0,199}$")

MAX_RESULT_WINDOW = 10_000  # Elasticsearch default: page * limit must stay below this
RETRY_STATUSES = {429, 502, 503, 504}
MAX_ATTEMPTS = 3
MAX_RETRY_WAIT = 30.0

_sleep = asyncio.sleep  # indirection so tests can skip real waiting


class ApiError(Exception):
    """data.gv.at could not be reached or returned an unexpected response."""


class NotFoundError(ApiError):
    """The requested object does not exist."""


def validate_id(value: str, what: str = "id") -> str:
    value = (value or "").strip()
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"Invalid {what}: {value!r}")
    return value


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        try:
            dt = email.utils.parsedate_to_datetime(raw)
            return max(0.0, dt.timestamp() - time.time())
        except (TypeError, ValueError):
            return None


def user_agent(contact: str = "") -> str:
    ua = f"datagvat-mcp/{__version__} (self-hosted MCP server; httpx)"
    return f"{ua} contact: {contact}" if contact else ua


class DataGvAtClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        throttle: Throttle | None = None,
    ):
        self.settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.api_base.rstrip("/") + "/",
            timeout=settings.request_timeout,
            headers={"User-Agent": user_agent(settings.contact), "Accept": "application/json"},
            follow_redirects=True,
            transport=transport,
        )
        self._throttle = throttle or Throttle(
            settings.rate_limit, settings.rate_burst, settings.max_concurrency, settings.queue_timeout
        )
        self._cache = TTLCache(settings.cache_max_entries)
        self._inflight: dict[tuple, asyncio.Future] = {}
        self.requests_sent = 0

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ core

    async def get(self, path: str, params: dict[str, Any] | None = None, ttl: float | None = None) -> Any:
        """GET a JSON document, served from cache when possible.

        Concurrent identical requests share one upstream call.
        """
        params = {k: v for k, v in (params or {}).items() if v is not None}
        key = (path, tuple(sorted((k, str(v)) for k, v in params.items())))
        cached = self._cache.get(key)
        if cached is not MISSING:
            return cached
        if key in self._inflight:
            return await asyncio.shield(self._inflight[key])

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            data = await self._fetch(path, params)
        except BaseException as e:
            fut.set_exception(e)
            fut.exception()  # mark retrieved; waiters (if any) still get it
            raise
        else:
            self._cache.set(key, data, self.settings.cache_ttl if ttl is None else ttl)
            fut.set_result(data)
            return data
        finally:
            self._inflight.pop(key, None)

    async def _fetch(self, path: str, params: dict[str, Any]) -> Any:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                async with self._throttle.slot():
                    self.requests_sent += 1
                    resp = await self._http.get(path, params=params)
            except ThrottleTimeout as e:
                raise ApiError(
                    "This server is rate-limiting its requests to data.gv.at and the queue is full; "
                    "try again in a minute."
                ) from e
            except httpx.TimeoutException as e:
                if attempt < MAX_ATTEMPTS:
                    await _sleep(2**attempt)
                    continue
                raise ApiError("data.gv.at did not respond in time") from e
            except httpx.TransportError as e:
                if attempt < MAX_ATTEMPTS:
                    await _sleep(2**attempt)
                    continue
                raise ApiError(f"Could not reach data.gv.at: {e.__class__.__name__}") from e

            status = resp.status_code
            if status == 404:
                raise NotFoundError("not found")
            if status in RETRY_STATUSES:
                wait = _retry_after(resp)
                if wait is None:
                    wait = 2.0**attempt
                if status in (429, 503):
                    self._throttle.penalize(min(wait, MAX_RETRY_WAIT))
                logger.warning("data.gv.at answered %s for %s; attempt %d", status, path, attempt)
                if attempt < MAX_ATTEMPTS and wait <= MAX_RETRY_WAIT:
                    await _sleep(wait)
                    continue
                raise ApiError(f"data.gv.at is busy (HTTP {status}); try again later")
            if status >= 400:
                raise ApiError(f"data.gv.at returned HTTP {status}: {resp.text[:200]}")
            try:
                return resp.json()
            except ValueError as e:
                raise ApiError("data.gv.at returned a non-JSON response") from e
        raise ApiError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------- endpoints

    async def search(
        self,
        kind: str = "dataset",
        query: str | None = None,
        facets: dict[str, list[str]] | None = None,
        sort: str | None = None,
        limit: int = 10,
        page: int = 0,
        min_date: str | None = None,
        max_date: str | None = None,
        includes: list[str] | None = None,
        aggregation_fields: list[str] | None = None,
        extra: dict[str, Any] | None = None,
        ttl: float | None = None,
    ) -> dict[str, Any]:
        """Run /search and return the `result` object ({count, results, facets?})."""
        if page * limit >= MAX_RESULT_WINDOW:
            raise ValueError(f"page * limit must be below {MAX_RESULT_WINDOW}")
        params: dict[str, Any] = {"filters": kind, "limit": limit, "page": page}
        if query:
            params["q"] = query
        if facets:
            params["facets"] = json.dumps(facets, sort_keys=True, separators=(",", ":"))
        if sort:
            params["sort"] = sort
        if min_date:
            params["minDate"] = min_date
        if max_date:
            params["maxDate"] = max_date
        if includes:
            params["includes"] = ",".join(includes)
        if aggregation_fields:
            params["aggregation"] = "true"
            params["aggregationAllFields"] = "false"
            params["aggregationFields"] = ",".join(aggregation_fields)
        else:
            params["aggregation"] = "false"  # facets are huge (all EU languages); skip unless asked
        params.update(extra or {})
        data = await self.get("search", params, ttl=ttl)
        return data.get("result", {}) if isinstance(data, dict) else {}

    async def get_dataset(self, dataset_id: str) -> dict[str, Any]:
        dataset_id = validate_id(dataset_id, "dataset id")
        data = await self.get(f"datasets/{quote(dataset_id, safe='')}", ttl=self.settings.cache_ttl_static)
        return data.get("result", {}) if isinstance(data, dict) else {}

    async def get_catalogue(self, catalogue_id: str) -> dict[str, Any]:
        catalogue_id = validate_id(catalogue_id, "catalogue id")
        data = await self.get(f"catalogues/{quote(catalogue_id, safe='')}", ttl=self.settings.cache_ttl_static)
        return data.get("result", {}) if isinstance(data, dict) else {}

    async def list_vocabularies(self) -> list[str]:
        data = await self.get("vocabularies", ttl=self.settings.cache_ttl_static)
        return [v for v in data if isinstance(v, str)] if isinstance(data, list) else []

    async def vocabulary_terms(
        self, vocabulary_id: str, query: str | None = None, limit: int = 50, page: int = 0
    ) -> dict[str, Any]:
        vocabulary_id = validate_id(vocabulary_id, "vocabulary id")
        return await self.search(
            kind="vocabulary",
            query=query,
            limit=limit,
            page=page,
            extra={"vocabulary": vocabulary_id},
            ttl=self.settings.cache_ttl_static,
        )
