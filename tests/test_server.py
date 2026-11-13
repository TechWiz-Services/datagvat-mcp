"""End-to-end tests through a real MCP client session (in-memory transport)."""

import json

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CreateMessageResult, ErrorData, TextContent

from conftest import DATASET, DATASET_2, DATASET_3, instant_throttle
from datagvat_mcp.server import build_server

EXPECTED_TOOLS = {
    "search_datasets", "semantic_search_datasets", "get_dataset", "get_dataset_distributions", "list_catalogues",
    "get_catalogue", "find_related_datasets", "analyze_dataset_quality", "compare_datasets", "list_vocabularies",
    "get_vocabulary", "search_vocabulary_terms", "preview_schema", "preview_data",
}


@pytest.fixture
def server(services):
    return build_server(services.settings)


async def call(session, name, args=None):
    r = await session.call_tool(name, args or {})
    return r


def ok(result):
    assert not result.isError, result.content[0].text
    return result.structuredContent


def err(result) -> str:
    assert result.isError
    return result.content[0].text


async def test_lists_tools_resources_prompts(server):
    async with create_connected_server_and_client_session(server) as s:
        tools = (await s.list_tools()).tools
        assert {t.name for t in tools} == EXPECTED_TOOLS
        assert all(t.annotations.readOnlyHint for t in tools)
        assert all(t.description for t in tools)
        assert [str(r.uri) for r in (await s.list_resources()).resources] == ["datagvat://themes"]
        templates = {t.uriTemplate for t in (await s.list_resource_templates()).resourceTemplates}
        assert templates == {"datagvat://datasets/{dataset_id}", "datagvat://catalogues/{catalogue_id}"}
        prompts = {p.name for p in (await s.list_prompts()).prompts}
        assert prompts == {"find_datasets", "quality_audit", "compare_datasets", "catalogue_overview"}


async def test_search_datasets_maps_filters(server, fake_api):
    async with create_connected_server_and_client_session(server) as s:
        r = ok(await call(s, "search_datasets", {
            "query": "bevölkerung", "themes": ["soci"], "formats": ["csv", "CSV"], "catalogue_ids": ["l9"],
            "high_value_only": True, "min_date": "2025-01-01", "max_date": "2025-12-31",
            "sort_by": "title_asc", "limit": 2, "page": 1,
        }))
    p = fake_api.calls("/search")[0].url.params
    assert json.loads(p["facets"]) == {"categories": ["SOCI"], "format": ["CSV"], "catalog": ["l9"], "is_hvd": ["true"]}
    assert p["sort"] == "title.de+asc"
    assert (p["minDate"], p["maxDate"]) == ("2025-01-01T00:00:00Z", "2025-12-31T23:59:59Z")
    assert (p["limit"], p["page"], p["aggregation"]) == ("2", "1", "false")
    assert "includes" in p
    assert r["page"] == 1 and r["limit"] == 2
    assert {x["id"] for x in r["results"]} <= {DATASET["id"], DATASET_2["id"]}
    assert all(set(x) <= {"id", "title", "description", "publisher", "catalogue", "themes", "keywords", "formats",
                          "modified", "issued", "high_value_dataset", "url"} for x in r["results"])


async def test_search_with_facets_and_paging_info(server):
    async with create_connected_server_and_client_session(server) as s:
        r = ok(await call(s, "search_datasets", {"query": "wien", "include_facets": True}))
    assert r["count"] == 300 and r["has_more"] is True and len(r["results"]) == 3
    assert r["facets"]["catalog"][0]["id"] == "harvest-oh-old"
    assert r["facets"]["categories"][0] == {"id": "SOCI", "title": "Bevölkerung und Gesellschaft", "count": 461}


async def test_search_validation_errors(server, fake_api):
    async with create_connected_server_and_client_session(server) as s:
        assert "Unknown theme" in err(await call(s, "search_datasets", {"themes": ["FOO"]}))
        assert "first 10000" in err(await call(s, "search_datasets", {"limit": 50, "page": 200}))
        r = await call(s, "search_datasets", {"min_date": "01.01.2024"})
        assert r.isError
        assert "Invalid catalogue id" in err(await call(s, "search_datasets", {"catalogue_ids": ["../x"]}))
    assert not fake_api.calls("/search")


async def test_get_dataset_and_distributions(server, fake_api):
    async with create_connected_server_and_client_session(server) as s:
        d = ok(await call(s, "get_dataset", {"dataset_id": DATASET["id"]}))
        dist = ok(await call(s, "get_dataset_distributions", {"dataset_id": DATASET["id"]}))
    assert d["title"].startswith("Bevölkerung") and d["distribution_count"] == 2
    assert d["licenses"] == ["https://creativecommons.org/licenses/by/4.0/deed.de"]
    assert dist["count"] == 2 and dist["distributions"][0]["format"] == "CSV"
    assert len(fake_api.calls(f"/datasets/{DATASET['id']}")) == 1  # second tool call served from cache


async def test_get_dataset_errors(server):
    async with create_connected_server_and_client_session(server) as s:
        assert "not found" in err(await call(s, "get_dataset", {"dataset_id": "nope"}))
        assert "Invalid dataset id" in err(await call(s, "get_dataset", {"dataset_id": "../../etc/passwd"}))
        assert "not found" in err(await call(s, "get_dataset_distributions", {"dataset_id": "nope"}))


async def test_list_catalogues(server, fake_api):
    async with create_connected_server_and_client_session(server) as s:
        top = ok(await call(s, "list_catalogues", {"limit": 2}))
        found = ok(await call(s, "list_catalogues", {"query": "Wien"}))
    assert top["catalogues"] == [{"id": "harvest-oh-old", "title": "OHOLD Harvester", "dataset_count": 29806},
                                 {"id": "l9", "title": "Stadt Wien", "dataset_count": 780}]
    assert found["catalogues"][0]["id"] == "l9"  # sorted by dataset count, empty duplicate last
    first, second = fake_api.calls("/search")
    assert first.url.params["aggregationFields"] == "catalog"
    assert second.url.params["filters"] == "catalogue" and second.url.params["q"] == "Wien"


async def test_get_catalogue(server, fake_api):
    async with create_connected_server_and_client_session(server) as s:
        c = ok(await call(s, "get_catalogue", {"catalogue_id": "l9", "recent_datasets": 2}))
        assert "list_catalogues" in err(await call(s, "get_catalogue", {"catalogue_id": "wien"}))
    assert c["dataset_count"] == 780 and len(c["recent_datasets"]) == 2
    p = fake_api.calls("/search")[0].url.params
    assert json.loads(p["facets"]) == {"catalog": ["l9"]} and p["sort"] == "modified+desc"


async def test_related_quality_compare(server):
    async with create_connected_server_and_client_session(server) as s:
        rel = ok(await call(s, "find_related_datasets", {"dataset_id": DATASET["id"]}))
        q = ok(await call(s, "analyze_dataset_quality", {"dataset_id": DATASET_3["id"]}))
        cmp_ = ok(await call(s, "compare_datasets", {"dataset_ids": [DATASET["id"], DATASET_2["id"], "nope"]}))
        assert "not found" in err(await call(s, "find_related_datasets", {"dataset_id": "nope"}))
        assert "None of the datasets" in err(await call(s, "compare_datasets", {"dataset_ids": ["nope", "nope2"]}))
    assert [r["id"] for r in rel["related"]] == [DATASET_2["id"]]
    assert q["rating"] == "poor" and q["missing"]
    assert len(cmp_["datasets"]) == 2 and cmp_["not_found"] == ["nope"]
    assert cmp_["common_themes"] == ["SOCI"]
    assert set(cmp_["common_keywords"]) == {"bevölkerung", "wien"}


def sampler(text):
    async def callback(context, params):
        sampler.calls.append(params)
        return CreateMessageResult(role="assistant", model="test", content=TextContent(type="text", text=text))

    return callback


sampler.calls = []


async def test_semantic_search_uses_client_sampling(server, fake_api):
    sampler.calls.clear()
    cb = sampler('```json\n{"keywords": ["Radverkehr", "Zählstelle Graz"], "themes": ["TRAN"], "formats": ["csv"]}\n```')
    async with create_connected_server_and_client_session(server, sampling_callback=cb) as s:
        r = ok(await call(s, "semantic_search_datasets", {"natural_query": "bike counters in Graz"}))
    assert r["interpreted_as"] == {"method": "llm_expansion", "query": 'Radverkehr "Zählstelle Graz"',
                                   "themes": ["TRAN"], "formats": ["CSV"]}
    assert "bike counters in Graz" in sampler.calls[0].messages[0].content.text
    p = fake_api.calls("/search")[0].url.params
    assert json.loads(p["facets"]) == {"categories": ["TRAN"], "format": ["CSV"]}


async def test_semantic_search_drops_guessed_themes_if_nothing_found(server, fake_api):
    fake_api.empty_when_theme = {"JUST"}
    cb = sampler('{"keywords": ["Gericht"], "themes": ["JUST"]}')
    async with create_connected_server_and_client_session(server, sampling_callback=cb) as s:
        r = ok(await call(s, "semantic_search_datasets", {"natural_query": "Gerichtsdaten"}))
    assert r["interpreted_as"]["themes"] == [] and r["count"] > 0
    assert len(fake_api.calls("/search")) == 2


async def test_semantic_search_falls_back_without_sampling(server, fake_api):
    async with create_connected_server_and_client_session(server) as s:
        r = ok(await call(s, "semantic_search_datasets", {"natural_query": "Luftgüte Linz"}))
    assert r["interpreted_as"]["method"] == "plain_text"
    assert fake_api.calls("/search")[0].url.params["q"] == "Luftgüte Linz"


async def test_semantic_search_falls_back_on_bad_llm_output(server):
    async with create_connected_server_and_client_session(server, sampling_callback=sampler("I don't know")) as s:
        r = ok(await call(s, "semantic_search_datasets", {"natural_query": "Luftgüte"}))
    assert r["interpreted_as"]["method"] == "plain_text"


async def test_vocabulary_tools(server, fake_api):
    async with create_connected_server_and_client_session(server) as s:
        v = ok(await call(s, "list_vocabularies"))
        terms = ok(await call(s, "get_vocabulary", {"vocabulary_id": "data-theme"}))
        found = ok(await call(s, "search_vocabulary_terms", {"vocabulary_id": "data-theme", "query": "health"}))
        assert "not found" in err(await call(s, "get_vocabulary", {"vocabulary_id": "nope"}))
    assert v["vocabularies"] == ["eurovoc", "data-theme", "licence"]
    assert terms["count"] == 2 and terms["terms"][0] == {
        "id": "SOCI", "label": "Bevölkerung und Gesellschaft",
        "uri": "http://publications.europa.eu/resource/authority/data-theme/SOCI"}
    assert [t["id"] for t in found["terms"]] == ["HEAL"]


async def test_preview_by_dataset_id(server, preview_files):
    csv_bytes = "BEZIRK;ANTEIL\nInnere Stadt;12,5\nWähring;3,1\n".encode("cp1252")
    preview_files["https://files.example.org/bevoelkerung.csv"] = lambda r: httpx.Response(
        200, content=csv_bytes, headers={"content-type": "text/csv"})
    async with create_connected_server_and_client_session(server) as s:
        data = ok(await call(s, "preview_data", {"dataset_id": DATASET["id"], "max_rows": 5}))
        schema = ok(await call(s, "preview_schema", {"dataset_id": DATASET["id"]}))
    assert data["distribution"]["id"] == "dist-csv"
    assert data["encoding"] == "windows-1252" and data["delimiter"] == "semicolon"
    assert data["rows"] == [["Innere Stadt", "12,5"], ["Währing", "3,1"]]
    assert "rows" not in schema
    assert schema["columns"][1] == {"name": "ANTEIL", "type": "number (decimal comma)", "empty_in_sample": 0,
                                    "examples": ["12,5", "3,1"]}


async def test_preview_by_url_and_errors(server, preview_files):
    preview_files["https://h.example/x.json"] = lambda r: httpx.Response(200, json=[{"a": 1}, {"a": 2}])
    async with create_connected_server_and_client_session(server) as s:
        r = ok(await call(s, "preview_data", {"url": "https://h.example/x.json"}))
        assert r["format"] == "json" and r["rows"] == [{"a": 1}, {"a": 2}]
        assert "either url or dataset_id" in err(await call(s, "preview_data", {}))
        assert "404" in err(await call(s, "preview_data", {"url": "https://h.example/missing.csv"}))
        assert "Distribution 'zzz' not found" in err(
            await call(s, "preview_data", {"dataset_id": DATASET["id"], "distribution_id": "zzz"}))
        assert "No distribution" in err(await call(s, "preview_data", {"dataset_id": DATASET_3["id"]}))


async def test_preview_skips_dead_distribution_links(server, fake_api, preview_files):
    fake_api.datasets["two"] = {"id": "two", "title": {"de": "Zwei"}, "distributions": [
        {"id": "dead", "format": {"id": "CSV"}, "access_url": "https://h.example/dead.csv"},
        {"id": "pdf", "format": {"id": "PDF"}, "access_url": "https://h.example/x.pdf"},
        {"id": "alive", "format": {"id": "CSV"}, "download_url": "https://h.example/alive.csv"},
    ]}
    preview_files["https://h.example/alive.csv"] = lambda r: httpx.Response(200, content=b"a,b\n1,2\n")
    async with create_connected_server_and_client_session(server) as s:
        r = ok(await call(s, "preview_data", {"dataset_id": "two"}))
        assert r["distribution"]["id"] == "alive" and r["rows"] == [["1", "2"]]
        fake_api.datasets["dead"] = {"id": "dead", "distributions": [
            {"id": f"d{i}", "format": {"id": "CSV"}, "access_url": f"https://h.example/{i}.csv"} for i in range(5)]}
        msg = err(await call(s, "preview_data", {"dataset_id": "dead"}))
        assert "All candidate distributions failed" in msg and msg.count("404") == 3  # capped at 3 attempts


async def test_preview_refuses_binary_distribution(server, fake_api, preview_files):
    fake_api.datasets["xl"] = {"id": "xl", "distributions": [
        {"id": "x", "format": {"id": "CSV"}, "access_url": " https://h.example/really.xlsx "}]}
    preview_files["https://h.example/really.xlsx"] = lambda r: httpx.Response(
        200, content=b"PK\x03\x04" + b"\x00" * 50, headers={"content-type": "text/csv"})
    async with create_connected_server_and_client_session(server) as s:
        assert "ZIP archive or Office file" in err(await call(s, "preview_data", {"dataset_id": "xl"}))
        assert "ZIP archive" in err(await call(s, "preview_schema", {"url": " https://h.example/really.xlsx\n"}))


# ----------------------------------------------------------- input validation


@pytest.mark.parametrize(
    "tool,args",
    [
        ("search_datasets", {"limit": 0}),
        ("search_datasets", {"limit": 51}),
        ("search_datasets", {"page": -1}),
        ("search_datasets", {"query": "x" * 501}),
        ("search_datasets", {"sort_by": "random"}),
        ("search_datasets", {"max_date": "2025-13"}),
        ("search_datasets", {"themes": "SOCI; DROP"}),
        ("semantic_search_datasets", {"natural_query": "x"}),
        ("semantic_search_datasets", {}),
        ("get_dataset", {}),
        ("get_dataset", {"dataset_id": "a/b"}),
        ("get_dataset", {"dataset_id": "x" * 201}),
        ("get_dataset_distributions", {"dataset_id": "?x=1"}),
        ("list_catalogues", {"limit": 101}),
        ("get_catalogue", {"catalogue_id": "l9", "recent_datasets": 21}),
        ("get_catalogue", {"catalogue_id": "l9/../x"}),
        ("find_related_datasets", {"dataset_id": "x", "limit": 31}),
        ("find_related_datasets", {"dataset_id": "x", "min_score": 101}),
        ("analyze_dataset_quality", {"dataset_id": "%2e%2e"}),
        ("compare_datasets", {"dataset_ids": ["only-one"]}),
        ("compare_datasets", {"dataset_ids": ["a", "b", "c", "d", "e", "f"]}),
        ("compare_datasets", {"dataset_ids": ["ok", "../x"]}),
        ("get_vocabulary", {"vocabulary_id": "data-theme", "limit": 201}),
        ("get_vocabulary", {"vocabulary_id": "a b"}),
        ("search_vocabulary_terms", {"vocabulary_id": "eurovoc", "query": ""}),
        ("search_vocabulary_terms", {"vocabulary_id": "eurovoc"}),
        ("preview_data", {"url": "https://h.example/x.csv", "max_rows": 101}),
        ("preview_data", {"url": "https://h.example/x.csv", "format": "xml"}),
        ("preview_data", {"dataset_id": "../x"}),
        ("preview_schema", {"url": "file:///etc/passwd"}),
        ("preview_schema", {"url": "gopher://h.example/x"}),
    ],
)
async def test_invalid_input_is_rejected_before_any_request(server, fake_api, preview_files, tool, args):
    preview_files["https://h.example/x.csv"] = lambda r: pytest.fail("must not download")
    async with create_connected_server_and_client_session(server) as s:
        assert err(await call(s, tool, args))
    assert not fake_api.requests


async def test_ids_are_trimmed(server, fake_api):
    async with create_connected_server_and_client_session(server) as s:
        c = ok(await call(s, "get_catalogue", {"catalogue_id": "  l9\n"}))
        d = ok(await call(s, "get_dataset", {"dataset_id": f" {DATASET['id']} "}))
    assert c["id"] == "l9" and d["id"] == DATASET["id"]
    assert json.loads(fake_api.calls("/search")[0].url.params["facets"]) == {"catalog": ["l9"]}


async def test_paging_stops_at_the_result_window(server, fake_api):
    async with create_connected_server_and_client_session(server) as s:
        last = ok(await call(s, "search_datasets", {"limit": 50, "page": 5}))  # fake API reports 300 hits
        assert last["has_more"] is False
        assert ok(await call(s, "search_datasets", {"limit": 50, "page": 4}))["has_more"] is True
        ok(await call(s, "search_datasets", {"limit": 50, "page": 199}))  # results 9951-10000: still allowed
        assert "first 10000" in err(await call(s, "search_datasets", {"limit": 50, "page": 200}))
        assert "first 10000" in err(await call(s, "search_datasets", {"limit": 30, "page": 333}))
    assert len(fake_api.calls("/search")) == 3


async def test_compare_ignores_duplicate_ids(server, fake_api):
    async with create_connected_server_and_client_session(server) as s:
        r = ok(await call(s, "compare_datasets", {"dataset_ids": [DATASET["id"], DATASET["id"], DATASET_2["id"]]}))
    assert [d["id"] for d in r["datasets"]] == [DATASET["id"], DATASET_2["id"]]
    assert len(fake_api.requests) == 2


async def test_labels_follow_the_configured_language(server, services, fake_api):
    services.settings.language = "en"
    async with create_connected_server_and_client_session(server) as s:
        r = ok(await call(s, "search_datasets", {"query": "x", "sort_by": "title_desc", "include_facets": True}))
    titles = {x["id"]: x["title"] for x in r["results"]}
    assert titles[DATASET_2["id"]] == "Population forecast Vienna"  # English label exists
    assert titles[DATASET["id"]].startswith("Bevölkerung")  # falls back to German
    assert r["facets"]["categories"][0]["title"] == "Population and society"
    assert fake_api.calls("/search")[0].url.params["sort"] == "title.en+desc"


# ------------------------------------------------- answers we do not control


WEIRD_DATASET = {
    "id": "weird", "title": ["Liste", "statt", "Objekt"], "description": 12345, "publisher": "Stadt Wien",
    "catalog": "l9", "categories": {"id": "SOCI"}, "keywords": "bevölkerung, wien", "distributions": "keine",
    "contact_point": "post@example.org", "accrual_periodicity": ["ANNUAL"], "temporal": "2008/2025",
    "language": "de", "provenance": {"label": "x"}, "catalog_record": [], "modified": 20251120, "is_hvd": "yes",
    "landing_page": {"resource": "https://example.org"},
}


async def test_tools_survive_a_dataset_of_unexpected_shape(server, fake_api):
    fake_api.raw["datasets/weird"] = {"result": WEIRD_DATASET}
    async with create_connected_server_and_client_session(server) as s:
        d = ok(await call(s, "get_dataset", {"dataset_id": "weird"}))
        dists = ok(await call(s, "get_dataset_distributions", {"dataset_id": "weird"}))
        q = ok(await call(s, "analyze_dataset_quality", {"dataset_id": "weird"}))
        rel = ok(await call(s, "find_related_datasets", {"dataset_id": "weird"}))
        cmp_ = ok(await call(s, "compare_datasets", {"dataset_ids": ["weird", DATASET["id"]]}))
        assert "No distribution" in err(await call(s, "preview_data", {"dataset_id": "weird"}))
        res = json.loads((await s.read_resource("datagvat://datasets/weird")).contents[0].text)
    assert d["id"] == "weird" and d["distribution_count"] == 0 and res["id"] == "weird"
    assert dists.pop("cache")["stale"] is False
    assert dists == {"dataset_id": "weird", "title": "Liste", "count": 0, "distributions": []}
    assert q["rating"] == "poor" and 0 <= q["score"] <= 100
    assert rel["related"] == []
    assert [x["id"] for x in cmp_["datasets"]] == ["weird", DATASET["id"]]


@pytest.mark.parametrize("body", [
    [], "oops", {"result": None}, {"result": []}, {"result": {"count": None, "results": None, "facets": None}},
    {"result": {"count": "many", "results": "none", "facets": "none"}},
    {"result": {"count": -1, "results": {"id": "lonely"}, "facets": {"id": "catalog", "items": "x"}}},
    {"result": {"count": 3, "results": [None, "x", 5, ["y"], {}], "facets": [None, 5, {"id": 7, "items": [{}]}]}},
])
async def test_tools_survive_search_answers_of_unexpected_shape(server, fake_api, body):
    fake_api.raw["search"] = body
    async with create_connected_server_and_client_session(server) as s:
        found = ok(await call(s, "search_datasets", {"query": "x", "include_facets": True}))
        ok(await call(s, "semantic_search_datasets", {"natural_query": "irgendwas"}))
        ok(await call(s, "list_catalogues"))
        ok(await call(s, "list_catalogues", {"query": "Wien"}))
        ok(await call(s, "get_catalogue", {"catalogue_id": "l9"}))
        ok(await call(s, "search_vocabulary_terms", {"vocabulary_id": "eurovoc", "query": "x"}))
        ok(await call(s, "find_related_datasets", {"dataset_id": DATASET["id"]}))
        vocab = await call(s, "get_vocabulary", {"vocabulary_id": "data-theme"})
        assert not vocab.isError or "not found or empty" in vocab.content[0].text
    assert isinstance(found["count"], int) and found["count"] >= 0 and isinstance(found["results"], list)
    assert all(isinstance(r, dict) for r in found["results"])


async def test_empty_document_is_reported_as_not_found(server, fake_api):
    fake_api.raw["datasets/ghost"] = {"result": None}
    fake_api.raw["catalogues/ghost"] = {"result": {}}
    async with create_connected_server_and_client_session(server) as s:
        assert "'ghost' not found" in err(await call(s, "get_dataset", {"dataset_id": "ghost"}))
        assert "'ghost' not found" in err(await call(s, "get_catalogue", {"catalogue_id": "ghost"}))


async def test_upstream_trouble_becomes_a_readable_tool_error(server, fake_api, no_retry_sleep):
    fake_api.respond = lambda request: httpx.Response(429, headers={"Retry-After": "600"})
    async with create_connected_server_and_client_session(server) as s:
        msg = err(await call(s, "search_datasets", {"query": "x"}))
    assert "data.gv.at is busy (HTTP 429)" in msg and "Traceback" not in msg
    assert len(fake_api.requests) == 1 and no_retry_sleep == []  # a 10-minute Retry-After is not waited out


# ---------------------------------------------------------------------- cache


async def test_answers_say_when_they_are_saved_copies(server, fake_api):
    async with create_connected_server_and_client_session(server) as s:
        first = ok(await call(s, "search_datasets", {"query": "wien"}))
        second = ok(await call(s, "search_datasets", {"query": "wien"}))
        partly = ok(await call(s, "compare_datasets", {"dataset_ids": [DATASET["id"], DATASET_2["id"]]}))
        again = ok(await call(s, "compare_datasets", {"dataset_ids": [DATASET["id"], DATASET_2["id"]]}))
    assert "cache" not in first and "cache" not in partly  # fetched just now
    assert second["cache"]["stale"] is False and second["cache"]["saved_copy_from"].endswith("Z")
    assert again["cache"]["stale"] is False
    assert len(fake_api.requests) == 3


async def test_saved_copy_is_served_when_data_gv_at_is_down(server, services, fake_api, no_retry_sleep):
    async with create_connected_server_and_client_session(server) as s:
        good = ok(await call(s, "get_dataset", {"dataset_id": DATASET["id"]}))
        services.client.cache._clock = lambda: __import__("time").time() + 8 * 86400  # a week later: expired
        fake_api.respond = lambda request: httpx.Response(503, headers={"Retry-After": "0"})
        stale = ok(await call(s, "get_dataset", {"dataset_id": DATASET["id"]}))
        unknown = err(await call(s, "get_dataset", {"dataset_id": DATASET_2["id"]}))  # nothing saved: an error
        fake_api.respond = None
        fresh = ok(await call(s, "get_dataset", {"dataset_id": DATASET["id"]}))
    note = stale.pop("cache")
    assert stale == good and note["stale"] is True and "could not be reached" in note["warning"]
    assert "busy" in unknown
    assert "cache" not in fresh  # back online: fetched again


async def test_expired_copy_is_not_served_while_data_gv_at_says_the_dataset_is_gone(server, services, fake_api):
    async with create_connected_server_and_client_session(server) as s:
        ok(await call(s, "get_dataset", {"dataset_id": DATASET["id"]}))
        services.client.cache._clock = lambda: __import__("time").time() + 8 * 86400
        del fake_api.datasets[DATASET["id"]]
        assert "not found" in err(await call(s, "get_dataset", {"dataset_id": DATASET["id"]}))


async def test_refresh_fetches_again_but_not_within_five_minutes(server, services, fake_api):
    import time

    async with create_connected_server_and_client_session(server) as s:
        ok(await call(s, "get_dataset", {"dataset_id": DATASET["id"]}))
        hurried = ok(await call(s, "get_dataset", {"dataset_id": DATASET["id"], "refresh": True}))
        assert "cache" in hurried and len(fake_api.requests) == 1  # the copy is seconds old: no new request
        services.client.cache._clock = lambda: time.time() + 301
        fake_api.datasets[DATASET["id"]]["title"] = {"de": "Neuer Titel"}
        plain = ok(await call(s, "get_dataset", {"dataset_id": DATASET["id"]}))
        renewed = ok(await call(s, "get_dataset", {"dataset_id": DATASET["id"], "refresh": True}))
        ok(await call(s, "search_datasets", {"query": "x"}))
        services.client.cache._clock = lambda: time.time() + 700
        ok(await call(s, "search_datasets", {"query": "x", "refresh": True}))
    assert plain["title"].startswith("Bevölkerung") and renewed["title"] == "Neuer Titel" and "cache" not in renewed
    assert len(fake_api.calls("/search")) == 2


async def test_preview_uses_and_refreshes_the_cache(server, services, preview_files):
    import time

    hits = []

    def handler(request):
        hits.append(1)
        if len(hits) == 3:
            return httpx.Response(502)
        return httpx.Response(200, content=f"a,b\n{len(hits)},2\n".encode())

    preview_files["https://h.example/f.csv"] = handler
    async with create_connected_server_and_client_session(server) as s:
        one = ok(await call(s, "preview_data", {"url": "https://h.example/f.csv"}))
        two = ok(await call(s, "preview_data", {"url": "https://h.example/f.csv"}))
        services.client.cache._clock = lambda: time.time() + 600
        three = ok(await call(s, "preview_data", {"url": "https://h.example/f.csv", "refresh": True}))
        services.client.cache._clock = lambda: time.time() + 3 * 3600  # expired, and the host answers 502
        four = ok(await call(s, "preview_schema", {"url": "https://h.example/f.csv"}))
    assert one["rows"] == [["1", "2"]] and "cache" not in one
    assert two["rows"] == [["1", "2"]] and two["cache"]["stale"] is False
    assert three["rows"] == [["2", "2"]] and "cache" not in three
    assert four["cache"]["stale"] is True and four["columns"][0]["examples"] == ["2"]


async def test_broken_answers_are_not_kept(server, fake_api):
    fake_api.raw["datasets/" + DATASET["id"]] = {"result": None}
    fake_api.raw["search"] = "oops"
    async with create_connected_server_and_client_session(server) as s:
        err(await call(s, "get_dataset", {"dataset_id": DATASET["id"]}))
        ok(await call(s, "search_datasets", {"query": "wien"}))
        fake_api.raw.clear()  # data.gv.at is fine again: the bad answers must not stick for days
        assert ok(await call(s, "get_dataset", {"dataset_id": DATASET["id"]}))["id"] == DATASET["id"]
        assert ok(await call(s, "search_datasets", {"query": "wien"}))["count"] == 300


async def test_cache_file_keeps_answers_across_a_restart(tmp_path, fake_api):
    from conftest import make_settings
    from datagvat_mcp.client import DataGvAtClient

    settings = make_settings(cache_dir=str(tmp_path))
    first = DataGvAtClient(settings, transport=httpx.MockTransport(fake_api), throttle=instant_throttle())
    await first.get_dataset(DATASET["id"])
    await first.close()
    second = DataGvAtClient(settings, transport=httpx.MockTransport(fake_api), throttle=instant_throttle())
    assert (await second.get_dataset(DATASET["id"]))["id"] == DATASET["id"]
    assert len(fake_api.requests) == 1 and (tmp_path / "cache.sqlite3").exists()
    await second.close()


async def test_cache_can_be_switched_off_completely(tmp_path, fake_api, no_retry_sleep):
    from conftest import make_settings
    from datagvat_mcp.server import make_services

    svc = make_services(make_settings(cache_enabled=False, cache_dir=str(tmp_path)))
    svc.client._http = httpx.AsyncClient(base_url=svc.settings.api_base + "/", transport=httpx.MockTransport(fake_api))
    try:
        assert svc.preview.cache is svc.client.cache and not svc.client.cache.persistent
        await svc.client.get_dataset(DATASET["id"])
        await svc.client.get_dataset(DATASET["id"])
        await svc.client.search(query="x")
        await svc.client.search(query="x")
        assert len(fake_api.requests) == 4  # every call goes upstream
        fake_api.respond = lambda request: httpx.Response(503, headers={"Retry-After": "0"})
        with pytest.raises(Exception, match="busy"):  # and there is no saved copy to fall back on
            await svc.client.get_dataset(DATASET["id"])
        assert list(tmp_path.iterdir()) == []  # no cache file is written
    finally:
        await svc.client.close()


# -------------------------------------------------------------- semantic search


async def test_semantic_search_falls_back_when_sampling_fails(server, fake_api):
    async def broken(context, params):
        return ErrorData(code=-32603, message="model unavailable")

    async with create_connected_server_and_client_session(server, sampling_callback=broken) as s:
        r = ok(await call(s, "semantic_search_datasets", {"natural_query": "Luftgüte Linz"}))
    assert r["interpreted_as"]["method"] == "plain_text" and len(fake_api.calls("/search")) == 1


async def test_semantic_search_falls_back_when_sampling_is_too_slow(server, monkeypatch):
    import asyncio

    from datagvat_mcp import semantic

    monkeypatch.setattr(semantic, "SAMPLING_TIMEOUT", 0.05)

    async def slow(context, params):
        await asyncio.sleep(5)

    async with create_connected_server_and_client_session(server, sampling_callback=slow) as s:
        r = ok(await call(s, "semantic_search_datasets", {"natural_query": "Luftgüte Linz"}))
    assert r["interpreted_as"]["method"] == "plain_text"


async def test_semantic_search_merges_user_filters_with_the_models(server, fake_api):
    cb = sampler('{"keywords": ["Luft \\"güte\\"", "Linz"], "themes": ["ENVI", "HEAL", "NOPE"], "formats": ["json"]}')
    async with create_connected_server_and_client_session(server, sampling_callback=cb) as s:
        r = ok(await call(s, "semantic_search_datasets", {
            "natural_query": "air quality in Linz", "themes": ["heal"], "formats": ["CSV"], "limit": 3, "page": 1}))
    assert r["interpreted_as"] == {"method": "llm_expansion", "query": '"Luft  güte" Linz',
                                   "themes": ["HEAL", "ENVI"], "formats": ["CSV", "JSON"]}
    p = fake_api.calls("/search")[0].url.params
    assert (p["limit"], p["page"]) == ("3", "1")


# ------------------------------------------------------------- server wiring


async def test_files_on_data_gv_at_count_against_the_api_rate_limit():
    from conftest import make_settings
    from datagvat_mcp.server import make_services

    svc = make_services(make_settings(api_base="https://www.data.gv.at/api/hub/search", request_interval=0.5,
                                      preview_request_interval=5, queue_timeout=30, contact="ops@example.org"))
    try:
        api = svc.client.throttle
        assert (api.interval, api.queue_timeout) == (0.5, 30)
        assert svc.preview.throttles.for_host("www.data.gv.at") is api
        assert svc.preview.throttles.for_host("data.gv.at") is api
        other = svc.preview.throttles.for_host("files.example.org")
        # A faster API setting does not speed up downloads from publishers' servers.
        assert other is not api and (other.interval, other.queue_timeout) == (5, 30)
        assert "ops@example.org" in svc.preview.user_agent
    finally:
        await svc.client.close()


async def test_tool_list_stays_small_and_documented(server):
    async with create_connected_server_and_client_session(server) as s:
        tools = (await s.list_tools()).tools
    payload = json.dumps([t.model_dump(mode="json", exclude_none=True) for t in tools])
    # Every connected agent pays for this text in its context window on each conversation.
    assert len(payload) < 18_000, len(payload)  # about 14 KB today
    for t in tools:
        assert len(t.description) >= 40, t.name
        assert t.inputSchema["type"] == "object"
        assert t.annotations.destructiveHint is False and t.annotations.idempotentHint is True


async def test_resource_errors_and_prompt_arguments(server):
    async with create_connected_server_and_client_session(server) as s:
        for uri in ("datagvat://datasets/nope", "datagvat://catalogues/nope", "datagvat://datasets/..%2F..",
                    "datagvat://unknown"):
            with pytest.raises(Exception):  # noqa: B017 - reported to the client as a JSON-RPC error
                await s.read_resource(uri)
        find = await s.get_prompt("find_datasets", {"topic": "Radverkehr Graz"})
        overview = await s.get_prompt("catalogue_overview", {"catalogue_id": "l9"})
        with pytest.raises(Exception):  # noqa: B017 - missing required argument
            await s.get_prompt("quality_audit", {})
    assert "Radverkehr Graz" in find.messages[0].content.text
    assert "catalogue_ids=['l9']" in overview.messages[0].content.text


async def test_resources_and_prompts(server):
    async with create_connected_server_and_client_session(server) as s:
        themes = json.loads((await s.read_resource("datagvat://themes")).contents[0].text)
        ds = json.loads((await s.read_resource(f"datagvat://datasets/{DATASET['id']}")).contents[0].text)
        cat = json.loads((await s.read_resource("datagvat://catalogues/l9")).contents[0].text)
        prompt = await s.get_prompt("compare_datasets", {"dataset_ids": "a, b ,c"})
        audit = await s.get_prompt("quality_audit", {"dataset_id": "x1"})
    assert len(themes) == 14 and themes["OP_DATPRO"]["en"] == "Provisional data"
    assert ds["id"] == DATASET["id"] and cat["id"] == "l9"
    assert "a, b, c" in prompt.messages[0].content.text
    assert "x1" in audit.messages[0].content.text
