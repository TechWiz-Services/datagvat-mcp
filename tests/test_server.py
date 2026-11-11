"""End-to-end tests through a real MCP client session (in-memory transport)."""

import json

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CreateMessageResult, TextContent

from conftest import DATASET, DATASET_2, DATASET_3
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
