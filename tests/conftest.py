"""Shared fixtures: realistic sample documents and a fake data.gv.at API."""

import asyncio
import copy
import json
from collections.abc import Callable

import httpx
import pytest

from datagvat_mcp import client as client_module
from datagvat_mcp.client import DataGvAtClient
from datagvat_mcp.config import Settings
from datagvat_mcp.preview import PreviewFetcher
from datagvat_mcp.server import Services, set_services
from datagvat_mcp.throttle import HostThrottles, Throttle

API = "https://api.test/api/hub/search"

# Shaped like real hub-search documents (trimmed).
DATASET = {
    "id": "98b782ca-8e46-43d7-a061-e196d0e0160a",
    "title": {"de": "Bevölkerung nach Geburtsbundesland seit 2008 - Wien"},
    "description": {"de": "Bevölkerung nach Geschlecht, Alter und <b>Geburtsbundesland</b> seit 2008 - Wien.<br>\nQuelle: MA23"},
    "publisher": {"type": "Agent", "name": "Stadt Wien", "email": "mailto:open@post.wien.gv.at",
                  "homepage": "https://digitales.wien.gv.at"},
    "catalog": {"id": "l9", "title": {"de": "Stadt Wien"}, "description": {"de": "OGD Wien"}},
    "categories": [{"id": "SOCI", "label": {"de": "Bevölkerung und Gesellschaft", "en": "Population and society",
                                            "fi": "Väestö ja yhteiskunta"},
                    "resource": "http://publications.europa.eu/resource/authority/data-theme/SOCI"}],
    "keywords": [{"id": "bevölkerung", "label": "Bevölkerung", "language": "de"},
                 {"id": "geburtsort", "label": "Geburtsort", "language": "de"},
                 {"id": "bundesland", "label": "Bundesland", "language": "de"},
                 {"id": "wien", "label": "Wien", "language": "de"}],
    "distributions": [
        {"id": "dist-csv", "title": {"de": "Bevölkerung CSV"},
         "format": {"id": "CSV", "label": "CSV", "resource": "http://publications.europa.eu/resource/authority/file-type/CSV"},
         "access_url": ["https://files.example.org/bevoelkerung.csv"],
         "license": {"resource": "https://creativecommons.org/licenses/by/4.0/deed.de"},
         "description": {"de": "NUTS | NUTS2-Region <br>\nREF_YEAR | Referenzjahr"}},
        {"id": "dist-pdf", "title": {"de": "Beschreibung"}, "format": {"id": "PDF", "label": "PDF"},
         "access_url": "https://files.example.org/doku.pdf",
         "license": {"resource": "https://creativecommons.org/licenses/by/4.0/deed.de"}},
    ],
    "contact_point": [{"name": "MA 23", "email": "mailto:post@ma23.wien.gv.at", "type": "Organization"}],
    "accrual_periodicity": {"label": "annual", "resource": "http://publications.europa.eu/resource/authority/frequency/ANNUAL"},
    "temporal": [{"gte": "2008-01-01", "lte": "2025-01-01"}],
    "language": [{"id": "de", "label": "German"}],
    "provenance": [{"label": "Statistik Austria (POPREG), aufbereitet von der MA23."}],
    "catalog_record": {"issued": "2025-12-16T13:31:42Z", "modified": "2025-12-16T13:31:42Z"},
    "modified": "2025-11-20",
    "issued": None,
    "is_hvd": False,
    "quality_meas": None,
    "resource": "https://www.data.gv.at/katalog/datasets/98b782ca-8e46-43d7-a061-e196d0e0160a",
}

DATASET_2 = {
    "id": "85e0c43a-78a6-45e5-a578-ea7739665936",
    "title": {"de": "Bevölkerungsprognose Wien", "en": "Population forecast Vienna"},
    "description": {"de": "Prognose der Bevölkerung Wiens."},
    "publisher": {"name": "Stadt Wien"},
    "catalog": {"id": "l9", "title": {"de": "Stadt Wien"}},
    "categories": [{"id": "SOCI"}, {"id": "REGI"}],
    "keywords": [{"label": "Bevölkerung"}, {"label": "Wien"}, {"label": "Prognose"}],
    "distributions": [{"id": "d2", "format": {"id": "JSON"}, "access_url": "https://files.example.org/p.json",
                       "license": {"resource": "https://creativecommons.org/licenses/by/4.0/"}}],
    "modified": "2026-01-10",
    "is_hvd": True,
}

DATASET_3 = {  # unrelated, sparse metadata
    "id": "minimal-1",
    "title": {"en": "Budget 2026"},
    "categories": [{"id": "ECON"}],
    "keywords": [],
    "distributions": [{"id": "d3", "format": {"id": "XLSX"}}],
    "modified": "2019-03-01",
}

CATALOGUE = {
    "id": "l9", "title": {"de": "Stadt Wien"}, "description": {"de": "Open Government Data (OGD) in Wien ..."},
    "count": 780, "language": [{"id": "de"}], "is_part_of": "data-gv-at",
    "modified": "2026-02-12T07:58:10Z", "issued": "2026-02-12T07:58:10Z", "publisher": None, "homepage": None,
}

FACETS = [
    {"id": "dataScope", "title": "Data scope", "items": [None, None]},
    {"id": "catalog", "title": "Catalogues", "items": [
        {"count": 780, "id": "l9", "title": "Stadt Wien"},
        {"count": 29806, "id": "harvest-oh-old", "title": "OHOLD Harvester"},
        {"count": 0, "id": "empty", "title": "Empty"}]},
    {"id": "categories", "title": "Categories", "items": [
        {"count": 461, "id": "SOCI", "title": {"de": "Bevölkerung und Gesellschaft", "en": "Population and society"}}]},
]

VOCAB_TERMS = [
    {"id": "SOCI", "pref_label": {"de": "Bevölkerung und Gesellschaft", "en": "Population and society"},
     "resource": "http://publications.europa.eu/resource/authority/data-theme/SOCI"},
    {"id": "HEAL", "pref_label": {"de": "Gesundheit", "en": "Health"},
     "resource": "http://publications.europa.eu/resource/authority/data-theme/HEAL"},
]


class FakeClock:
    """Monotonic clock that only advances when something sleeps on it."""

    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds
        await asyncio.sleep(0)


def instant_throttle() -> Throttle:
    """One request at a time but no pause: tests must not wait (Settings rejects intervals below 0.5 s)."""
    return Throttle(0, 5)


def make_settings(**overrides) -> Settings:
    base = dict(
        api_base=API, request_interval=0.5, queue_timeout=5,  # 0.5 s is the lowest value Settings accepts
        cache_dir="", contact="", auth_token=None, allowed_hosts="",
    )
    base.update(overrides)
    return Settings(**base)


class FakeApi:
    """Callable for httpx.MockTransport emulating the hub-search endpoints used by the client."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.datasets = {d["id"]: copy.deepcopy(d) for d in (DATASET, DATASET_2, DATASET_3)}
        self.empty_when_theme: set[str] = set()
        self.raw: dict[str, object] = {}  # path -> JSON body returned as is (for malformed answers)
        self.respond: Callable[[httpx.Request], httpx.Response] | None = None  # answers everything if set

    def calls(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith(path)]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/hub/search/")
        p = request.url.params
        if self.respond:
            return self.respond(request)
        if path in self.raw:
            return httpx.Response(200, json=self.raw[path])
        if path == "search":
            kind = p.get("filters")
            limit = int(p.get("limit", 10))
            if kind == "dataset":
                facets = json.loads(p["facets"]) if "facets" in p else {}
                results = list(self.datasets.values())
                if set(facets.get("categories", [])) & self.empty_when_theme:
                    results = []
                if "catalog" in facets:
                    results = [d for d in results if (d.get("catalog") or {}).get("id") in facets["catalog"]]
                body = {"index": "dataset", "count": len(results) * 100, "results": results[:limit]}
                if p.get("aggregation") == "true":
                    body["facets"] = FACETS
                return httpx.Response(200, json={"result": body})
            if kind == "catalogue":
                cats = [{"id": "at-vkz-l9", "title": {"de": "Stadt Wien"}, "count": 0}, CATALOGUE]
                return httpx.Response(200, json={"result": {"index": "catalogue", "count": 2, "results": cats[:limit]}})
            if kind == "vocabulary":
                if p.get("vocabulary") == "nope":
                    return httpx.Response(200, json={"result": {"count": 0, "results": []}})
                terms = VOCAB_TERMS
                if p.get("q"):
                    terms = [t for t in terms if p["q"].lower() in json.dumps(t, ensure_ascii=False).lower()]
                return httpx.Response(200, json={"result": {"index": "vocabulary", "count": len(terms),
                                                            "results": terms[:limit]}})
        if path.startswith("datasets/"):
            ds = self.datasets.get(path.split("/", 1)[1])
            return httpx.Response(200, json={"result": ds}) if ds else httpx.Response(404, text="Document not found")
        if path == "catalogues/l9":
            return httpx.Response(200, json={"result": CATALOGUE})
        if path.startswith("catalogues/"):
            return httpx.Response(404, text="Document not found")
        if path == "vocabularies":
            return httpx.Response(200, json=["eurovoc", "data-theme", "licence"])
        return httpx.Response(500, text=f"unexpected path {path}")


@pytest.fixture
def no_retry_sleep(monkeypatch):
    waits = []

    async def fake_sleep(s):
        waits.append(s)

    monkeypatch.setattr(client_module, "_sleep", fake_sleep)
    return waits


@pytest.fixture
def fake_api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def preview_files() -> dict[str, Callable[[httpx.Request], httpx.Response]]:
    """URL -> handler for preview downloads; tests add entries."""
    return {}


@pytest.fixture
async def services(settings, fake_api, preview_files, no_retry_sleep):
    def preview_handler(request: httpx.Request) -> httpx.Response:
        handler = preview_files.get(str(request.url))
        return handler(request) if handler else httpx.Response(404)

    client = DataGvAtClient(settings, transport=httpx.MockTransport(fake_api), throttle=instant_throttle())
    svc = Services(
        settings=settings,
        client=client,
        preview=PreviewFetcher(
            max_bytes=settings.preview_max_bytes,
            timeout=5,
            throttles=HostThrottles(0, 5),
            cache=client.cache,  # one cache for both, as in production
            transport_factory=lambda: httpx.MockTransport(preview_handler),
        ),
    )
    set_services(svc)
    yield svc
    await svc.client.close()
    set_services(None)
