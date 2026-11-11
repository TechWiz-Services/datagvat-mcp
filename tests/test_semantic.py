from datagvat_mcp.semantic import build_query, expand_query, parse_expansion


def test_parse_plain_json():
    r = parse_expansion('{"keywords": ["Gesundheit", "Wien"], "themes": ["heal", "XXX"], "formats": ["csv"]}')
    assert r == {"keywords": ["Gesundheit", "Wien"], "themes": ["HEAL"], "formats": ["CSV"]}


def test_parse_fenced_json_with_prose():
    text = 'Sure! Here you go:\n```json\n{"keywords": ["Radverkehr"], "themes": [], "formats": []}\n```'
    assert parse_expansion(text)["keywords"] == ["Radverkehr"]


def test_parse_rejects_garbage():
    assert parse_expansion("no json here") is None
    assert parse_expansion('{"keywords": []}') is None
    assert parse_expansion('{"keywords": "notalist"}') is None
    assert parse_expansion("{broken") is None
    assert parse_expansion("") is None


def test_parse_limits_and_dedupes():
    r = parse_expansion('{"keywords": ["a","a","b","c","d","e","f","g","h","i"], "formats": ["CSV; DROP TABLE"]}')
    assert r["keywords"] == ["a", "b", "c", "d", "e", "f", "g"]
    assert r["formats"] == []


def test_build_query_quotes_phrases():
    assert build_query(["Wien", "öffentlicher Verkehr", 'bad"quote']) == 'Wien "öffentlicher Verkehr" "bad quote"'


async def test_expand_query_without_context():
    assert await expand_query(None, "anything") is None
