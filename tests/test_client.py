import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from conftest import DATASET, FakeClock, make_settings
from datagvat_mcp import client as client_module
from datagvat_mcp.client import ApiError, DataGvAtClient, NotFoundError, user_agent, validate_id
from datagvat_mcp.throttle import Throttle


def fake_throttle(interval: float = 0) -> Throttle:
    clock = FakeClock()
    return Throttle(interval, clock=clock, sleep=clock.sleep)


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
    c = make_client(fake_api, cache_dataset_ttl=0)
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
    responses = iter([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={"result": {"id": "x"}})])
    throttle = fake_throttle()
    c = DataGvAtClient(make_settings(), transport=httpx.MockTransport(lambda r: next(responses)), throttle=throttle)
    assert await c.get_dataset("x") == {"id": "x"}
    assert no_retry_sleep == [7.0]
    assert throttle._clock() >= 7  # the retry itself waited in the throttle: every other request backs off too
    assert c.requests_sent == 2
    await c.close()


async def test_retry_after_as_http_date(no_retry_sleep, monkeypatch):
    # "now" is Fri, 15 Jan 2027 08:00:00 GMT
    monkeypatch.setattr(client_module, "time", SimpleNamespace(time=lambda: 1_800_000_000.0))
    responses = iter([httpx.Response(503, headers={"Retry-After": "Fri, 15 Jan 2027 08:00:12 GMT"}),
                      httpx.Response(200, json={"result": {"id": "x"}})])
    c = make_client(lambda r: next(responses))
    assert await c.get_dataset("x") == {"id": "x"}
    assert no_retry_sleep == [12.0]
    await c.close()


@pytest.mark.parametrize("header", ["soon", "", "-5"])
async def test_unusable_retry_after_falls_back_to_backoff(no_retry_sleep, header):
    responses = iter([httpx.Response(429, headers={"Retry-After": header}), httpx.Response(200, json={"result": {"id": "x"}})])
    c = make_client(lambda r: next(responses))
    assert await c.get_dataset("x") == {"id": "x"}
    assert no_retry_sleep == ([0.0] if header == "-5" else [2.0])
    await c.close()


@pytest.mark.parametrize("status", [502, 504])
async def test_gateway_errors_are_retried_without_penalising_everyone(no_retry_sleep, status):
    responses = iter([httpx.Response(status), httpx.Response(200, json={"result": {"id": "x"}})])
    throttle = fake_throttle()
    c = DataGvAtClient(make_settings(), transport=httpx.MockTransport(lambda r: next(responses)), throttle=throttle)
    assert await c.get_dataset("x") == {"id": "x"}
    assert no_retry_sleep == [2.0] and throttle._next_allowed == 0
    await c.close()


async def test_timeouts_are_retried_then_reported(no_retry_sleep):
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    c = make_client(slow)
    with pytest.raises(ApiError, match="did not respond in time"):
        await c.get_dataset("x")
    assert c.requests_sent == 3 and no_retry_sleep == [2, 4]
    await c.close()


async def test_failures_are_not_cached(no_retry_sleep):
    responses = iter([httpx.Response(500, text="boom"), httpx.Response(200, json={"result": {"id": "x"}})])
    c = make_client(lambda r: next(responses))
    with pytest.raises(ApiError):
        await c.get_dataset("x")
    assert await c.get_dataset("x") == {"id": "x"}
    await c.close()


async def test_requests_never_overlap_and_are_spaced():
    """The politeness guarantee end to end: one connection, a pause after each response."""
    clock = FakeClock()
    active = 0
    starts, ends = [], []

    async def handler(request):
        nonlocal active
        active += 1
        assert active == 1, "two requests to data.gv.at were in flight at the same time"
        starts.append(clock.t)
        await clock.sleep(0.5)  # the response takes half a second
        ends.append(clock.t)
        active -= 1
        return httpx.Response(200, json={"result": {"id": str(request.url)}})

    c = DataGvAtClient(make_settings(), transport=httpx.MockTransport(handler),
                       throttle=Throttle(2, queue_timeout=600, clock=clock, sleep=clock.sleep))
    await asyncio.gather(*(c.get_dataset(f"ds-{i}") for i in range(6)))
    assert len(starts) == 6
    gaps = [round(s - e, 6) for e, s in zip(ends, starts[1:])]
    assert all(g >= 2 for g in gaps), gaps
    await c.close()


async def test_cancelled_caller_does_not_fail_the_others_or_waste_the_request():
    calls = 0
    release = asyncio.Event()

    async def slow(request):
        nonlocal calls
        calls += 1
        await release.wait()
        return httpx.Response(200, json={"result": {"id": "x"}})

    c = make_client(slow)
    first = asyncio.create_task(c.get_dataset("x"))
    second = asyncio.create_task(c.get_dataset("x"))
    await asyncio.sleep(0.01)
    first.cancel()  # e.g. this caller's MCP client disconnected
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await second == {"id": "x"}
    assert await c.get_dataset("x") == {"id": "x"}  # ...and the answer was cached
    assert calls == 1
    await c.close()


async def test_request_finishes_and_is_cached_even_if_every_caller_is_cancelled():
    release = asyncio.Event()
    calls = 0

    async def slow(request):
        nonlocal calls
        calls += 1
        await release.wait()
        return httpx.Response(200, json={"result": {"id": "x"}})

    c = make_client(slow)
    only = asyncio.create_task(c.get_dataset("x"))
    await asyncio.sleep(0.01)
    only.cancel()
    with pytest.raises(asyncio.CancelledError):
        await only
    release.set()
    assert await c.get_dataset("x") == {"id": "x"}
    assert calls == 1 and not c._inflight
    await c.close()


async def test_coalesced_callers_all_see_the_same_error():
    async def broken(request):
        await asyncio.sleep(0.01)
        return httpx.Response(500, text="boom")

    c = make_client(broken)
    results = await asyncio.gather(*(c.get_dataset("x") for _ in range(3)), return_exceptions=True)
    assert all(isinstance(r, ApiError) for r in results) and c.requests_sent == 1
    assert not c._inflight
    await c.close()


async def test_close_cancels_requests_in_flight():
    async def never(request):
        await asyncio.Event().wait()

    c = make_client(never)
    pending = asyncio.create_task(c.get_dataset("x"))
    await asyncio.sleep(0.01)
    await c.close()
    with pytest.raises(asyncio.CancelledError):
        await pending


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


@pytest.mark.parametrize("body", [None, [], "text", 7, {}, {"result": None}, {"result": []}, {"result": "x"},
                                  {"result": {}}])
async def test_answers_of_unexpected_shape(body):
    c = make_client(lambda r: httpx.Response(200, content=json.dumps(body).encode()))
    assert await c.search(query="x") == {}
    assert await c.vocabulary_terms("data-theme") == {}
    assert await c.list_vocabularies() == []
    for lookup in (c.get_dataset, c.get_catalogue):
        with pytest.raises(NotFoundError):  # "200 OK" without a document
            await lookup("x")
    await c.close()


async def test_vocabulary_list_keeps_only_names():
    c = make_client(lambda r: httpx.Response(200, json=["eurovoc", None, 5, {"id": "x"}, "licence"]))
    assert await c.list_vocabularies() == ["eurovoc", "licence"]
    await c.close()


async def test_queue_full_is_reported():
    async def slow(request):
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={"result": {"id": "x"}})

    c = DataGvAtClient(make_settings(), transport=httpx.MockTransport(slow),
                       throttle=Throttle(0, queue_timeout=0.05))
    results = await asyncio.gather(c.get_dataset("a"), c.get_dataset("b"), return_exceptions=True)
    assert any(isinstance(r, ApiError) and "queue" in str(r) for r in results)
    await c.close()


async def test_each_upstream_request_is_logged_at_debug_level(fake_api, caplog):
    c = make_client(fake_api)
    with caplog.at_level("DEBUG", logger="datagvat_mcp.client"):
        await c.get_dataset(DATASET["id"])
        await c.get_dataset(DATASET["id"])  # cached: no request, no line
    lines = [r.getMessage() for r in caplog.records if r.name == "datagvat_mcp.client"]
    assert len(lines) == 1 and lines[0].startswith(f"GET datasets/{DATASET['id']} -> 200 in ")
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
