"""Smoke tests against the real data.gv.at API.

Skipped by default (the Docker build runs offline-safe tests only). Run with:
  DATAGVAT_LIVE_TESTS=1 python -m pytest tests/test_live.py
They send ~6 throttled requests and verify the API still looks the way this
server expects (endpoint paths, field names).
"""

import os

import pytest

from datagvat_mcp.client import DataGvAtClient
from datagvat_mcp.config import Settings
from datagvat_mcp.normalize import detail_dataset, summarize_dataset

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("DATAGVAT_LIVE_TESTS") != "1", reason="set DATAGVAT_LIVE_TESTS=1"),
]


@pytest.fixture
async def client():
    c = DataGvAtClient(Settings(contact="live-test"))
    yield c
    await c.close()


async def test_search_and_dataset_shape(client):
    r = await client.search(query="bevölkerung AND wien", limit=2)
    assert r["count"] > 0
    first = r["results"][0]
    assert {"id", "title", "distributions", "catalog"} <= set(first)
    s = summarize_dataset(first)
    assert s["title"] and s["catalogue"]["id"]
    d = detail_dataset(await client.get_dataset(first["id"]))
    assert d["distributions"] and (d["distributions"][0].get("access_url") or d["distributions"][0].get("download_url"))


async def test_catalogue_and_facets(client):
    cat = await client.get_catalogue("l9")
    assert cat["count"] > 0
    r = await client.search(limit=0, aggregation_fields=["catalog"])
    assert any(f.get("id") == "catalog" for f in r.get("facets", []))


async def test_vocabularies(client):
    assert "data-theme" in await client.list_vocabularies()
    r = await client.vocabulary_terms("data-theme", limit=20)
    assert {t["id"] for t in r["results"]} >= {"HEAL", "TRAN", "SOCI"}
