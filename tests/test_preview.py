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
    return PreviewFetcher(max_bytes=max_bytes, timeout=5, throttles=HostThrottles(0, 5),
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


async def test_failed_downloads_are_not_cached():
    responses = iter([httpx.Response(503), httpx.Response(200, content=b"a,b\n1,2\n")])
    f = make_fetcher(lambda r: next(responses))
    with pytest.raises(PreviewError, match="HTTP 503"):
        await f.fetch("https://h/a.csv")
    assert (await f.fetch("https://h/a.csv")).content == b"a,b\n1,2\n"


async def test_fetch_follows_redirects_and_reports_the_final_url():
    def handler(request):
        if request.url.path == "/old.csv":
            return httpx.Response(301, headers={"Location": "https://cdn.example/new.csv"})
        return httpx.Response(200, content=b"a,b\n1,2\n", headers={"content-type": "text/csv"})

    f = await make_fetcher(handler).fetch("https://h.example/old.csv")
    assert f.final_url == "https://cdn.example/new.csv" and f.content == b"a,b\n1,2\n"


async def test_fetch_gives_up_on_redirect_loops():
    f = make_fetcher(lambda r: httpx.Response(302, headers={"Location": str(r.url)}))
    with pytest.raises(PreviewError, match="Download failed"):
        await f.fetch("https://h.example/loop.csv")


async def test_fetch_reports_connection_problems():
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(PreviewError, match="Could not connect"):
        await make_fetcher(refuse).fetch("https://h.example/x.csv")


@pytest.mark.parametrize("url", ["", "x.csv", "//h/x.csv", "ftp://h/x.csv", "javascript:alert(1)", "https:///x.csv",
                                 "data:text/csv,a"])
async def test_fetch_rejects_urls_that_are_not_absolute_http(url):
    def handler(request):
        pytest.fail("must not send a request")

    with pytest.raises(PreviewError, match="absolute http"):
        await make_fetcher(handler).fetch(url)


@pytest.mark.parametrize("url", ["https://h.example/\x00.csv", "https://h.example:99999/x.csv", "https://[::1/x.csv",
                                 "https://h.example/a\nb.csv", "http://h.example:port/x.csv"])
async def test_fetch_reports_malformed_urls_cleanly(url):
    with pytest.raises(PreviewError):
        await make_fetcher(lambda r: httpx.Response(200, content=b"a\n1\n")).fetch(url)


async def test_fetch_trims_and_encodes_urls_from_metadata():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=b"a\n1\n")

    f = make_fetcher(handler)
    await f.fetch("  https://h.example/Bevölkerung 2024.csv\n")
    await f.fetch("https://h.example/Bevölkerung 2024.csv")  # same file: served from the cache
    assert seen == ["https://h.example/Bev%C3%B6lkerung%202024.csv"]


async def test_redirect_to_a_malformed_address_is_a_clean_error():
    def handler(request):
        if request.url.path == "/x.csv":
            return httpx.Response(302, headers={"Location": "https://h.example:port/moved.csv"})
        return httpx.Response(200, content=b"a\n1\n")

    with pytest.raises(PreviewError, match="Download failed"):
        await make_fetcher(handler).fetch("https://h.example/x.csv")


def blocking_fetcher(handler, blocked=("ris.bka.gv.at", "data.bka.gv.at")) -> PreviewFetcher:
    return PreviewFetcher(max_bytes=1000, timeout=5, throttles=HostThrottles(0, 5), blocked_hosts=blocked,
                          transport_factory=lambda: httpx.MockTransport(handler))


@pytest.mark.parametrize("url", [
    "https://data.bka.gv.at/ris/api/v2.6/Bundesrecht?Suchworte=x",
    "https://www.ris.bka.gv.at/Dokumente/x.csv",
    "http://RIS.BKA.GV.AT/x.json",
    "https://ris.bka.gv.at./x.json",
    "https://deep.sub.data.bka.gv.at:8443/x",
])
async def test_blocked_hosts_are_never_contacted(url):
    def handler(request):
        pytest.fail("must not send a request")

    with pytest.raises(PreviewError, match="are disabled on this server"):
        await blocking_fetcher(handler).fetch(url)


async def test_redirect_to_a_blocked_host_is_refused():
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(302, headers={"Location": "https://www.ris.bka.gv.at/Dokumente/x.csv"})

    with pytest.raises(PreviewError, match="ris.bka.gv.at are disabled"):
        await blocking_fetcher(handler).fetch("https://files.example.org/moved.csv")
    assert seen == ["files.example.org"]


async def test_similar_host_names_and_an_empty_block_list_are_allowed():
    ok = lambda r: httpx.Response(200, content=b"a\n1\n")  # noqa: E731
    f = blocking_fetcher(ok)
    for url in ("https://notris.bka.gv.at/x.csv", "https://bka.gv.at/x.csv", "https://ris.bka.gv.at.example.org/x.csv"):
        assert (await f.fetch(url)).content == b"a\n1\n"
    assert (await blocking_fetcher(ok, blocked=()).fetch("https://www.ris.bka.gv.at/x.csv")).content == b"a\n1\n"


async def test_downloads_from_one_host_wait_for_each_other_but_not_for_other_hosts():
    from conftest import FakeClock
    from datagvat_mcp.throttle import Throttle

    clock = FakeClock()
    throttles = HostThrottles(2, 60)
    for host in ("a.example", "b.example"):
        throttles.register(host, Throttle(2, 60, clock=clock, sleep=clock.sleep))
    started = []

    def handler(request):
        started.append((request.url.host, clock.t))
        return httpx.Response(200, content=b"a\n1\n")

    f = PreviewFetcher(max_bytes=1000, timeout=5, throttles=throttles,
                       transport_factory=lambda: httpx.MockTransport(handler))
    for url in ("https://a.example/1.csv", "https://b.example/1.csv", "https://a.example/2.csv"):
        await f.fetch(url)
    assert started == [("a.example", 0), ("b.example", 0), ("a.example", 2)]


# ------------------------------------------------- files that are not CSV/JSON


@pytest.mark.parametrize(
    "content,kind",
    [
        (b"PK\x03\x04\x14\x00\x06\x00[Content_Types].xml", "ZIP archive or Office file"),
        (b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj", "PDF document"),
        (b"\x1f\x8b\x08\x00\x00\x00\x00\x00", "gzip archive"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "legacy Office file"),
        (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", "binary file"),
        (b"PAR1\x15\x04\x15\x00\x00", "binary file"),
    ],
)
@pytest.mark.parametrize("forced", [None, "csv", "json"])
def test_binary_files_are_refused_even_with_a_forced_format(content, kind, forced):
    # Without this check an XLSX served as text/csv was decoded as Windows-1252 and returned as garbage rows.
    with pytest.raises(PreviewError, match=kind.replace("(", r"\(")):
        parse(content, True, "https://x/data.csv", "text/csv", forced, 5)


def test_html_page_under_a_csv_url_is_not_parsed_as_csv():
    page = b"<!DOCTYPE html>\n<html><body>Bitte anmelden, um fortzufahren</body></html>"
    assert detect_format("https://x/export.csv", "text/csv", page.decode()) is None
    with pytest.raises(PreviewError, match="Only CSV and JSON"):
        parse(page, False, "https://x/export.csv", "text/csv", None, 5)


@pytest.mark.parametrize("codec", ["utf-16", "utf-16-le", "utf-16-be"])
def test_utf16_csv_as_exported_by_excel(codec):
    text = "Gemeinde\tEinwohner\nGänserndorf\t11.000\nWörgl\t14.000\n"
    bom = {"utf-16-le": b"\xff\xfe", "utf-16-be": b"\xfe\xff"}.get(codec, b"")
    r = parse(bom + text.encode(codec), False, "https://x/f.csv", None, None, 5)
    assert r["encoding"] == "utf-16" and r["delimiter"] == "tab"
    assert r["rows"] == [["Gänserndorf", "11.000"], ["Wörgl", "14.000"]]


def test_utf16_download_cut_in_the_middle_of_a_character():
    data = b"\xff\xfe" + "a;b\n1;2\n3;4".encode("utf-16-le")
    r = parse(data[:-1], True, "https://x/f.csv", None, None, 5)
    assert r["rows"] == [["1", "2"]]


def test_very_long_cell_does_not_break_the_preview():
    # Python's csv module refuses fields over 128 KB by default; WKT geometry columns exceed that.
    wkt = "POLYGON((" + ",".join(f"{i} {i}" for i in range(40_000)) + "))"
    assert len(wkt) > 200_000
    r = parse_csv(f'id;geom\n1;"{wkt}"\n2;"POINT(1 1)"\n', partial=False, max_rows=5)
    assert [row[0] for row in r["rows"]] == ["1", "2"]
    assert len(r["rows"][0][1]) == 300 and r["columns"][1]["type"] == "string"


def test_csv_with_a_single_column_and_blank_lines():
    r = parse(b"Bezirk\n\nMitte\n\nNord\n", False, "https://x/bezirke.csv", None, None, 5)
    assert r["columns"] == [{"name": "Bezirk", "type": "string", "empty_in_sample": 0, "examples": ["Mitte", "Nord"]}]
    assert r["rows"] == [["Mitte"], ["Nord"]]


def test_csv_reports_malformed_rows_and_empty_cells():
    r = parse_csv("a,b,c\n1,,3\n1,2\n4,5,6,7\n7,,9\n", partial=False, max_rows=10)
    assert r["malformed_rows_skipped"] == 2
    assert r["rows"] == [["1", "", "3"], ["7", "", "9"]]
    assert r["columns"][1] == {"name": "b", "type": "empty", "empty_in_sample": 2, "examples": []}


def test_csv_without_a_header_row():
    with pytest.raises(PreviewError, match="no header"):
        parse_csv(";;\n1;2;3\n", partial=False, max_rows=5)


def test_csv_types_are_inferred_from_a_sample_not_from_the_whole_file():
    text = "n\n" + "".join(f"{i}\n" for i in range(60)) + "not a number\n"
    r = parse_csv(text, partial=False, max_rows=100)
    assert r["rows_sampled_for_types"] == 50 and r["columns"][0]["type"] == "integer"
    assert len(r["rows"]) == 61 and r["more_rows_available"] is False


def test_max_rows_is_clamped():
    text = "n\n" + "".join(f"{i}\n" for i in range(500))
    assert len(parse(text.encode(), False, "https://x/n.csv", None, None, 10_000)["rows"]) == 100
    assert len(parse(text.encode(), False, "https://x/n.csv", None, None, 0)["rows"]) == 1


# ------------------------------------------------------------ JSON edge cases


def test_json_with_mixed_and_null_columns():
    r = parse_json(json.dumps([{"a": 1, "b": None, "c": {"x": 1}}, {"a": "1", "b": None, "c": [1, 2]}]), False, 5)
    assert {c["name"]: c["type"] for c in r["columns"]} == {"a": "mixed", "b": "null", "c": "mixed"}


def test_json_scalars_and_plain_arrays_are_shown_as_content():
    r = parse_json("[1, 2, 3]", False, 5)
    assert (r["structure"], r["rows"], r["content"]) == ("array_of_values", [], [1, 2, 3])
    assert parse_json('"just a string"', False, 5)["structure"] == "scalar"
    big = parse_json(json.dumps(list(range(5000))), False, 5)
    assert isinstance(big["content"], str) and len(big["content"]) == 300  # never dumps a huge array


def test_json_nested_values_are_cut():
    r = parse_json(json.dumps([{"blob": {"k": "v" * 1000}, "text": "t" * 1000, "n": 1}]), False, 5)
    row = r["rows"][0]
    assert len(row["blob"]) == 300 and len(row["text"]) == 300 and row["n"] == 1


def test_geojson_features_without_properties_or_geometry():
    gj = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": None, "geometry": None},
        "not a feature",
        {"type": "Feature", "properties": {"a": 1}, "geometry": {"type": "Polygon", "coordinates": []}}]}
    r = parse_json(json.dumps(gj), False, 5)
    assert r["rows"] == [{}, {"a": 1, "geometry_type": "Polygon"}]


def test_truncated_json_inside_a_string_with_brackets():
    full = json.dumps({"features": [{"properties": {"note": "a } tricky ] \"value\" {", "id": i}} for i in range(200)]})
    for cut in (150, 333, 1000, 2500):
        data = recover_json(full[:cut])
        assert isinstance(data["features"], list) and data["features"]


def test_truncated_json_that_cannot_be_recovered():
    with pytest.raises(PreviewError, match="Invalid JSON"):
        recover_json('{"a": "' + "x" * 50)
    with pytest.raises(PreviewError, match="Invalid JSON"):
        recover_json("")


async def test_default_transport_blocks_internal_targets():
    fetcher = PreviewFetcher(max_bytes=1000, timeout=5, throttles=HostThrottles(0, 5))
    for url in ("http://127.0.0.1:8000/health", "http://169.254.169.254/latest/", "http://[::1]/"):
        with pytest.raises(PreviewError, match="private or reserved"):
            await fetcher.fetch(url)
