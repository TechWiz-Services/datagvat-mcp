import json

import httpx
import pytest

from datagvat_mcp.preview import (
    PreviewError,
    PreviewFetcher,
    decode_text,
    detect_format,
    infer_type,
    parse,
    parse_csv,
    parse_json,
    recover_json,
)
from datagvat_mcp.throttle import HostThrottles

# ---------------------------------------------------------------- decoding


def test_decode_utf8_with_bom():
    assert decode_text("﻿Name;Ort\nÄ;Wien".encode()) == ("Name;Ort\nÄ;Wien", "utf-8")


def test_decode_windows_1252_fallback():
    assert decode_text("Straße;Größe".encode("cp1252")) == ("Straße;Größe", "windows-1252")


def test_decode_utf8_cut_mid_character_when_partial():
    data = "abcß".encode()[:-1]  # download stopped inside the two-byte 'ß'
    assert decode_text(data, partial=True) == ("abc", "utf-8")


@pytest.mark.parametrize(
    "url,ctype,text,expected",
    [
        ("https://x/data", "text/csv", "", "csv"),
        ("https://x/data", "application/geo+json", "", "json"),
        ("https://x/data.CSV?x=1", None, "", "csv"),
        ("https://x/data.geojson", "application/octet-stream", "", "json"),
        ("https://x/gogv/l9ogd", "text/plain", "a;b;c\n1;2;3", "csv"),
        ("https://x/api", None, '  [{"a": 1}]', "json"),
        ("https://x/page", "text/html", "<html>", None),
        ("https://x/blob", None, "justtext", None),
    ],
)
def test_detect_format(url, ctype, text, expected):
    assert detect_format(url, ctype, text) == expected


@pytest.mark.parametrize(
    "values,expected",
    [
        (["1", "-2", "", "30"], "integer"),
        (["1.5", "2", "3e4"], "number"),
        (["1,5", "1.234,56", "7"], "number (decimal comma)"),
        (["ja", "Nein"], "boolean"),
        (["2024-01-01", "2024-02-01T10:00:00Z"], "date"),
        (["01.02.2024", "31.12.2023"], "date (dd.mm.yyyy)"),
        (["Wien", "1"], "string"),
        (["", " "], "empty"),
    ],
)
def test_infer_type(values, expected):
    assert infer_type(values) == expected


# --------------------------------------------------------------------- CSV

AUSTRIAN_CSV = "NUTS;BEZIRK;JAHR;ANTEIL;DATUM\nAT13;Innere Stadt;2024;12,5;01.01.2024\nAT13;Leopoldstadt;2024;7,25;01.01.2024\n"


def test_parse_semicolon_csv_with_decimal_comma():
    r = parse_csv(AUSTRIAN_CSV, partial=False, max_rows=10)
    assert r["delimiter"] == "semicolon"
    types = {c["name"]: c["type"] for c in r["columns"]}
    assert types == {"NUTS": "string", "BEZIRK": "string", "JAHR": "integer",
                     "ANTEIL": "number (decimal comma)", "DATUM": "date (dd.mm.yyyy)"}
    assert r["rows"][1] == ["AT13", "Leopoldstadt", "2024", "7,25", "01.01.2024"]
    assert r["more_rows_available"] is False
    assert r["columns"][1]["examples"] == ["Innere Stadt", "Leopoldstadt"]


def test_partial_csv_drops_cut_off_last_line():
    text = "a,b\n1,2\n3,4\n5,"
    r = parse_csv(text, partial=True, max_rows=10)
    assert r["rows"] == [["1", "2"], ["3", "4"]]
    assert r["more_rows_available"] is True


def test_csv_max_rows_and_malformed_rows():
    text = "a,b\n" + "".join(f"{i},{i}\n" for i in range(30)) + "bad\n"
    r = parse_csv(text, partial=False, max_rows=5)
    assert len(r["rows"]) == 5 and r["more_rows_available"] is True


def test_csv_quoted_fields_and_tabs():
    r = parse_csv('name\tnote\n"Müller, Hans"\t"a ""quoted"" word"\n', partial=False, max_rows=5)
    assert r["delimiter"] == "tab"
    assert r["rows"][0] == ["Müller, Hans", 'a "quoted" word']


def test_csv_empty_file():
    with pytest.raises(PreviewError):
        parse_csv("", partial=False, max_rows=5)


def test_long_cells_are_cut():
    r = parse_csv("a\n" + "x" * 1000 + "\n", partial=False, max_rows=5)
    assert len(r["rows"][0][0]) == 300


# -------------------------------------------------------------------- JSON


def test_json_array_of_objects():
    r = parse_json(json.dumps([{"a": 1, "b": "x"}, {"a": 2, "b": None}]), partial=False, max_rows=10)
    assert r["structure"] == "array_of_objects"
    assert {c["name"]: c["type"] for c in r["columns"]} == {"a": "integer", "b": "string"}
    assert r["rows"][0] == {"a": 1, "b": "x"}


def test_truncated_json_array_is_recovered():
    full = json.dumps([{"id": i, "name": f"n{i}"} for i in range(50)])
    r = parse_json(full[:400], partial=True, max_rows=100)
    assert 0 < len(r["rows"]) < 50 and r["more_rows_available"] is True


def test_truncated_nested_json_is_recovered():
    full = json.dumps({"meta": {"x": "}"}, "data": [{"id": i} for i in range(100)]})
    data = recover_json(full[:300])
    assert isinstance(data["data"], list) and data["data"]


def test_geojson_feature_collection():
    gj = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"NAME": "Prater"}, "geometry": {"type": "Point", "coordinates": [16, 48]}}]}
    r = parse_json(json.dumps(gj), partial=False, max_rows=10)
    assert r["structure"] == "geojson_features"
    assert r["rows"] == [{"NAME": "Prater", "geometry_type": "Point"}]


def test_json_records_under_key_and_single_object():
    assert parse_json('{"results": [{"a": 1}]}', False, 5)["structure"] == "objects_under_results"
    r = parse_json('{"version": 1, "name": "x"}', False, 5)
    assert r["structure"] == "single_object" and r["content"] == {"version": 1, "name": "x"}


def test_invalid_json():
    with pytest.raises(PreviewError):
        parse_json("{nope", partial=False, max_rows=5)


def test_parse_dispatch_and_unsupported():
    r = parse(AUSTRIAN_CSV.encode("cp1252"), False, "https://x/f", "text/csv", None, 2)
    assert r["format"] == "csv" and r["encoding"] == "utf-8" and len(r["rows"]) == 2
    assert parse(b'{"a":[{"b":1}]}', False, "https://x/f", None, "geojson", 5)["format"] == "json"
    with pytest.raises(PreviewError, match="Only CSV and JSON"):
        parse(b"<html></html>", False, "https://x/page", "text/html", None, 5)


# ------------------------------------------------------------------ fetching


def make_fetcher(handler, max_bytes=1000) -> PreviewFetcher:
    return PreviewFetcher(max_bytes=max_bytes, timeout=5, throttles=HostThrottles(100, 100, 5),
                          transport_factory=lambda: httpx.MockTransport(handler))


async def test_fetch_sends_range_and_identity_encoding():
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(206, content=b"a,b\n1,2\n", headers={"content-type": "text/csv; charset=utf-8"})

    f = await make_fetcher(handler).fetch("https://files.example.org/x.csv")
    assert (f.content, f.partial, f.content_type) == (b"a,b\n1,2\n", True, "text/csv")
    assert seen["range"] == "bytes=0-999"
    assert seen["accept-encoding"] == "identity"
    assert seen["user-agent"].startswith("datagvat-mcp/")


async def test_fetch_stops_reading_large_body():
    produced = 0

    async def body():
        nonlocal produced
        for _ in range(10_000):
            produced += 1
            yield b"x" * 65536

    f = await make_fetcher(lambda r: httpx.Response(200, content=body()), max_bytes=100_000).fetch("https://h/big")
    assert len(f.content) == 100_000 and f.partial is True
    assert produced <= 3


async def test_fetch_small_complete_file():
    f = await make_fetcher(lambda r: httpx.Response(200, content=b"tiny")).fetch("https://h/t.csv")
    assert (f.content, f.partial) == (b"tiny", False)


async def test_fetch_caps_oversized_206():
    f = await make_fetcher(lambda r: httpx.Response(206, content=b"y" * 5000), max_bytes=100).fetch("https://h/x")
    assert len(f.content) == 100


async def test_fetch_errors():
    with pytest.raises(PreviewError, match="404"):
        await make_fetcher(lambda r: httpx.Response(404)).fetch("https://h/missing")
    with pytest.raises(PreviewError, match="HTTP 500"):
        await make_fetcher(lambda r: httpx.Response(500)).fetch("https://h/err")

    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(PreviewError, match="timed out"):
        await make_fetcher(timeout).fetch("https://h/slow")
    with pytest.raises(PreviewError, match="absolute http"):
        await make_fetcher(lambda r: httpx.Response(200)).fetch("file:///etc/passwd")


async def test_fetch_results_are_cached():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"a")

    f = make_fetcher(handler)
    await f.fetch("https://h/a.csv")
    await f.fetch("https://h/a.csv")
    assert calls == 1


async def test_default_transport_blocks_internal_targets():
    fetcher = PreviewFetcher(max_bytes=1000, timeout=5, throttles=HostThrottles(100, 100, 5))
    for url in ("http://127.0.0.1:8000/health", "http://169.254.169.254/latest/", "http://[::1]/"):
        with pytest.raises(PreviewError, match="private or reserved"):
            await fetcher.fetch(url)
