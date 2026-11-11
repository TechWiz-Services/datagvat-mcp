import asyncio
import json

import httpx
import pytest

from conftest import DATASET, FakeClock, make_settings
from datagvat_mcp.client import ApiError, DataGvAtClient, NotFoundError, user_agent, validate_id
from datagvat_mcp.throttle import Throttle


def fake_throttle() -> Throttle:
    clock = FakeClock()
    return Throttle(100, 100, clock=clock, sleep=clock.sleep)


def make_client(handler, **settings_overrides) -> DataGvAtClient:
    return DataGvAtClient(make_settings(**settings_overrides), transport=httpx.MockTransport(handler),
                          throttle=fake_throttle())


async def test_search_builds_compact_request(fake_api):
    c = make_client(fake_api)
    await c.search(query="verkehr AND wien", facets={"format": ["CSV"], "categories": ["TRAN"]},
                   sort="modified+desc", limit=5, page=2, min_date="2026-01-01T00:00:00Z",
                   includes=["id", "title"])
    p = fake_api.requests[0].url.params
    assert p["filters"] == "dataset"
    assert p["q"] == "verkehr AND wien"
    assert json.loads(p["facets"]) == {"categories": ["TRAN"], "format": ["CSV"]}
    assert p["facets"] == '{"categories":["TRAN"],"format":["CSV"]}'  # stable -> cacheable
    assert p["sort"] == "modified+desc"
    assert (p["limit"], p["page"]) == ("5", "2")
    assert p["minDate"] == "2026-01-01T00:00:00Z"
    assert p["includes"] == "id,title"
    assert p["aggregation"] == "false"  # facets are huge; off unless requested
    assert "+" in str(fake_api.requests[0].url) or "%2B" in str(fake_api.requests[0].url)
    await c.close()


async def test_search_with_selected_facets(fake_api):
    c = make_client(fake_api)
    r = await c.search(limit=0, aggregation_fields=["catalog", "format"])
    p = fake_api.requests[0].url.params
    assert (p["aggregation"], p["aggregationAllFields"], p["aggregationFields"]) == ("true", "false", "catalog,format")
    assert r["facets"]
    await c.close()


async def test_search_rejects_pages_beyond_result_window(fake_api):
    c = make_client(fake_api)
    with pytest.raises(ValueError):
        await c.search(limit=50, page=200)
    assert not fake_api.requests
    await c.close()


async def test_responses_are_cached(fake_api):
    c = make_client(fake_api)
    a = await c.get_dataset(DATASET["id"])
    b = await c.get_dataset(DATASET["id"])
    assert a == b and a["id"] == DATASET["id"]
    assert len(fake_api.requests) == 1
    await c.search(query="x")
    await c.search(query="x")
    await c.search(query="y")
    assert len(fake_api.requests) == 3
    await c.close()


async def test_cache_disabled_with_zero_ttl(fake_api):
    c = make_client(fake_api, cache_ttl=0, cache_ttl_static=0)
    await c.get_dataset(DATASET["id"])
    await c.get_dataset(DATASET["id"])
    assert len(fake_api.requests) == 2
    await c.close()


async def test_identical_concurrent_requests_are_coalesced():
    calls = 0

    async def slow(request):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"result": {"id": "x"}})

    c = make_client(slow)
    results = await asyncio.gather(*(c.get_dataset("x") for _ in range(5)))
    assert calls == 1 and all(r == {"id": "x"} for r in results)
    await c.close()


async def test_not_found_is_raised_and_not_cached(fake_api):
    c = make_client(fake_api)
    for _ in range(2):
        with pytest.raises(NotFoundError):
            await c.get_dataset("missing")
    assert len(fake_api.requests) == 2
    await c.close()


async def test_retry_after_429_then_success(no_retry_sleep):
    responses = iter([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={"result": {}})])
    throttle = fake_throttle()
    c = DataGvAtClient(make_settings(), transport=httpx.MockTransport(lambda r: next(responses)), throttle=throttle)
    assert await c.get_dataset("x") == {}
    assert no_retry_sleep == [7.0]
    assert throttle._blocked_until > 0  # all other requests back off too
    assert c.requests_sent == 2
    await c.close()


async def test_gives_up_after_repeated_503(no_retry_sleep):
    c = make_client(lambda r: httpx.Response(503, headers={"Retry-After": "1"}))
    with pytest.raises(ApiError, match="busy"):
        await c.get_dataset("x")
    assert c.requests_sent == 3
    await c.close()


async def test_does_not_wait_for_very_long_retry_after(no_retry_sleep):
    c = make_client(lambda r: httpx.Response(429, headers={"Retry-After": "3600"}))
    with pytest.raises(ApiError):
        await c.get_dataset("x")
    assert c.requests_sent == 1 and no_retry_sleep == []
    await c.close()


async def test_server_error_is_not_retried():
    c = make_client(lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(ApiError, match="HTTP 500"):
        await c.get_dataset("x")
    assert c.requests_sent == 1
    await c.close()


async def test_network_errors_are_retried_then_reported(no_retry_sleep):
    def fail(request):
        raise httpx.ConnectError("down", request=request)

    c = make_client(fail)
    with pytest.raises(ApiError, match="Could not reach"):
        await c.get_dataset("x")
    assert c.requests_sent == 3 and no_retry_sleep == [2, 4]
    await c.close()


async def test_non_json_response():
    c = make_client(lambda r: httpx.Response(200, text="<html>"))
    with pytest.raises(ApiError, match="non-JSON"):
        await c.get_dataset("x")
    await c.close()


async def test_queue_full_is_reported():
    async def slow(request):
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={"result": {}})

    c = DataGvAtClient(make_settings(), transport=httpx.MockTransport(slow),
                       throttle=Throttle(100, 100, concurrency=1, queue_timeout=0.05))
    results = await asyncio.gather(c.get_dataset("a"), c.get_dataset("b"), return_exceptions=True)
    assert any(isinstance(r, ApiError) and "queue" in str(r) for r in results)
    await c.close()


async def test_user_agent_and_contact():
    seen = {}

    def handler(request):
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200, json=[])

    c = make_client(handler, contact="admin@example.org")
    await c.list_vocabularies()
    assert seen["ua"].startswith("datagvat-mcp/") and "admin@example.org" in seen["ua"]
    assert "contact" not in user_agent("")
    await c.close()


@pytest.mark.parametrize("good", ["98b782ca-8e46-43d7-a061-e196d0e0160a", "l9", "gga-31842", "OP_DATPRO",
                                  "data-theme", "a.b~c:d@e+f"])
def test_validate_id_accepts(good):
    assert validate_id(good) == good


@pytest.mark.parametrize("bad", ["", "../etc", "a/b", "a b", "-start", "x" * 201, "a?b", "a#b", "%2e%2e"])
def test_validate_id_rejects(bad):
    with pytest.raises(ValueError):
        validate_id(bad)


async def test_ids_are_url_encoded(fake_api):
    c = make_client(fake_api)
    with pytest.raises(NotFoundError):
        await c.get_dataset("urn:x@y")
    assert fake_api.requests[0].url.raw_path.endswith(b"/datasets/urn%3Ax%40y")
    await c.close()


async def test_vocabulary_terms_use_search_endpoint(fake_api):
    c = make_client(fake_api)
    r = await c.vocabulary_terms("data-theme", query="gesund", limit=5)
    p = fake_api.requests[0].url.params
    assert fake_api.requests[0].url.path.endswith("/search")
    assert (p["filters"], p["vocabulary"], p["q"]) == ("vocabulary", "data-theme", "gesund")
    assert [t["id"] for t in r["results"]] == ["HEAL"]
    await c.close()
