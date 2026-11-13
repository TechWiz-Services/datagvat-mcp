import json

import httpx

from conftest import DATASET, DATASET_2, DATASET_3, instant_throttle, make_settings
from datagvat_mcp.client import DataGvAtClient
from datagvat_mcp.similarity import features, find_related, similarity


def test_similarity_scoring():
    a, b, c = features(DATASET), features(DATASET_2), features(DATASET_3)
    score, why = similarity(a, b)
    # 1 shared theme (30) + 2 shared keywords (20) + same publisher (15)
    assert score == 65
    assert why == {"matching_themes": ["SOCI"], "matching_keywords": ["bevölkerung", "wien"], "same_publisher": True}
    assert similarity(a, c)[0] == 0


def test_similarity_is_capped():
    big = {"themes": {"A", "B", "C"}, "keywords": {str(i) for i in range(9)}, "publisher": "p"}
    assert similarity(big, big)[0] == 100


async def test_find_related_queries_by_keywords_within_themes(fake_api):
    client = DataGvAtClient(make_settings(), transport=httpx.MockTransport(fake_api), throttle=instant_throttle())
    r = await find_related(client, DATASET["id"], limit=5, min_score=20)
    search = fake_api.calls("/search")[0].url.params
    assert json.loads(search["facets"]) == {"categories": ["SOCI"]}
    assert set(search["q"].split()) == {"bevölkerung", "bundesland", "geburtsort", "wien"}
    ids = [x["id"] for x in r["related"]]
    assert DATASET["id"] not in ids  # source excluded
    assert ids == [DATASET_2["id"]]  # unrelated DATASET_3 below min_score
    assert r["related"][0]["similarity_score"] == 65
    assert r["source"]["title"].startswith("Bevölkerung")
    await client.close()


async def test_find_related_without_features(fake_api):
    client = DataGvAtClient(make_settings(), transport=httpx.MockTransport(fake_api), throttle=instant_throttle())
    fake_api.datasets["bare"] = {"id": "bare", "title": {"de": "x"}}
    r = await find_related(client, "bare")
    assert r["related"] == [] and "no themes" in r["note"]
    assert not fake_api.calls("/search")
    await client.close()
