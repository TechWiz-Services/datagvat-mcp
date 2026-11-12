"""Smoke tests against the real data.gv.at API.

Skipped by default (the Docker build runs offline-safe tests only). Run with:
  DATAGVAT_LIVE_TESTS=1 python -m pytest tests/test_live.py
They send about 18 requests, one at a time and spaced like every other
request of this server, and verify that the API still looks the way the
server expects (endpoint paths, parameters, field names).
"""

import os

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from datagvat_mcp.client import DataGvAtClient, NotFoundError
from datagvat_mcp.config import Settings
from datagvat_mcp.normalize import detail_dataset, normalize_facets, summarize_catalogue, summarize_dataset, vocab_term
from datagvat_mcp.quality import assess_quality
from datagvat_mcp.server import Services, build_server, make_services, set_services
from datagvat_mcp.throttle import Throttle

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("DATAGVAT_LIVE_TESTS") != "1", reason="set DATAGVAT_LIVE_TESTS=1"),
]

SETTINGS = Settings(contact=os.environ.get("DATAGVAT_CONTACT") or "datagvat-mcp live test")
_next_allowed = 0.0  # carries the pause over from one test (and its event loop) to the next


@pytest.fixture
async def client():
    global _next_allowed
    throttle = Throttle(SETTINGS.request_interval, SETTINGS.queue_timeout)
    throttle._next_allowed = _next_allowed
    c = DataGvAtClient(SETTINGS, throttle=throttle)
    yield c
    _next_allowed = throttle._next_allowed
    await c.close()


async def test_search_and_dataset_shape(client):
    r = await client.search(query="bevölkerung AND wien", limit=2)
    assert r["count"] > 0
    first = r["results"][0]
    assert {"id", "title", "distributions", "catalog"} <= set(first)
    s = summarize_dataset(first)
    assert s["title"] and s["catalogue"]["id"] and s["url"].endswith(first["id"])
    raw = await client.get_dataset(first["id"])
    d = detail_dataset(raw)
    assert d["distributions"] and (d["distributions"][0].get("access_url") or d["distributions"][0].get("download_url"))
    assert d["licenses"], "distributions no longer carry a license the server understands"
    assert 0 < assess_quality(raw)["score"] <= 100


async def test_portal_size_and_requested_fields_only(client):
    r = await client.search(limit=1, includes=["id", "title"])
    assert 20_000 < r["count"] < 500_000, "README says ~76,000 datasets"
    assert set(r["results"][0]) <= {"id", "title"}, "the includes parameter no longer trims documents"
    assert "facets" not in r or not r["facets"], "aggregation=false no longer suppresses facets"


async def test_filters_sorting_and_dates(client):
    r = await client.search(facets={"format": ["CSV"], "categories": ["TRAN"]}, sort="modified+desc", limit=3,
                            min_date="2020-01-01T00:00:00Z", includes=["id", "categories", "distributions", "modified"])
    assert r["count"] > 0
    for d in r["results"]:
        s = summarize_dataset(d)
        assert "TRAN" in s["themes"] and "CSV" in s["formats"]
    modified = [d["modified"] for d in r["results"] if d.get("modified")]
    assert modified == sorted(modified, reverse=True)
    by_title = await client.search(query="wien", sort="title.de+asc", limit=3, includes=["id", "title"])
    assert by_title["results"], "sorting by title.de is no longer accepted"
    hvd = await client.search(facets={"is_hvd": ["true"]}, limit=1, includes=["id", "is_hvd"])
    assert hvd["count"] > 0 and hvd["results"][0]["is_hvd"] is True


async def test_catalogue_and_facets(client):
    cat = await client.get_catalogue("l9")
    assert cat["count"] > 0 and summarize_catalogue(cat)["title"]
    r = await client.search(limit=0, aggregation_fields=["catalog"])
    top = normalize_facets(r.get("facets"), top=5).get("catalog")
    assert top and top[0]["count"] >= top[-1]["count"] and all(c["id"] and c["title"] for c in top)
    found = await client.search(kind="catalogue", query="Wien", limit=5, includes=["id", "title", "count"])
    assert "l9" in [c["id"] for c in found["results"]]


async def test_vocabularies(client):
    assert "data-theme" in await client.list_vocabularies()
    r = await client.vocabulary_terms("data-theme", limit=20)
    assert {t["id"] for t in r["results"]} >= {"HEAL", "TRAN", "SOCI", "OP_DATPRO"}
    assert vocab_term(next(t for t in r["results"] if t["id"] == "HEAL"), "en")["label"] == "Health"
    found = await client.vocabulary_terms("data-theme", query="Gesundheit", limit=5)
    assert "HEAL" in [t["id"] for t in found["results"]]


async def test_unknown_dataset_is_a_404(client):
    with pytest.raises(NotFoundError):
        await client.get_dataset("this-dataset-does-not-exist-0000")
    assert client.requests_sent == 1  # a 404 is not retried


async def test_tools_end_to_end_against_the_real_api(client):
    """A typical agent session through the MCP layer; only data.gv.at is contacted (no file previews)."""
    real = make_services(SETTINGS)
    await real.client.close()
    set_services(Services(settings=SETTINGS, client=client, preview=real.preview))
    try:
        async with create_connected_server_and_client_session(build_server(SETTINGS)) as s:
            found = (await s.call_tool("search_datasets", {"query": "luftgüte", "limit": 3})).structuredContent
            assert found["count"] > 0 and found["results"][0]["title"]
            ds_id = found["results"][0]["id"]
            detail = await s.call_tool("get_dataset", {"dataset_id": ds_id})
            assert not detail.isError and detail.structuredContent["distribution_count"] >= 1
            quality = await s.call_tool("analyze_dataset_quality", {"dataset_id": ds_id})  # served from the cache
            assert not quality.isError and quality.structuredContent["rating"] in ("good", "fair", "poor")
            related = await s.call_tool("find_related_datasets", {"dataset_id": ds_id, "limit": 3})
            assert not related.isError
            cats = await s.call_tool("list_catalogues", {"limit": 5})
            assert not cats.isError and len(cats.structuredContent["catalogues"]) == 5
    finally:
        set_services(None)
