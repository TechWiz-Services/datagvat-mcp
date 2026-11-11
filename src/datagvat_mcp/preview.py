"""Partial download and parsing of distribution files (CSV, JSON, GeoJSON).

Files live on many third-party hosts. Downloads go through PublicOnlyTransport
(SSRF guard), are throttled per host, capped in size, and cached briefly.
"""

import csv
import io
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from datagvat_mcp.cache import MISSING, TTLCache
from datagvat_mcp.client import user_agent
from datagvat_mcp.net_guard import PublicOnlyTransport
from datagvat_mcp.throttle import HostThrottles, ThrottleTimeout

MAX_PREVIEW_ROWS = 100
SAMPLE_ROWS_FOR_TYPES = 50
MAX_CELL_CHARS = 300
CSV_CONTENT_TYPES = {"text/csv", "application/csv", "text/comma-separated-values", "text/tab-separated-values"}
JSON_CONTENT_TYPES = {"application/json", "text/json", "application/geo+json", "application/ld+json"}


class PreviewError(Exception):
    pass


@dataclass
class Fetched:
    content: bytes
    partial: bool
    content_type: str | None
    final_url: str


class PreviewFetcher:
    def __init__(
        self,
        max_bytes: int,
        timeout: float,
        throttles: HostThrottles,
        cache_entries: int = 32,
        cache_ttl: float = 3600,
        contact: str = "",
        transport_factory: Callable[[], httpx.AsyncBaseTransport] = PublicOnlyTransport,
    ):
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.throttles = throttles
        self.cache = TTLCache(cache_entries)
        self.cache_ttl = cache_ttl
        self.user_agent = user_agent(contact)
        self.transport_factory = transport_factory

    async def fetch(self, url: str) -> Fetched:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise PreviewError("URL must be an absolute http:// or https:// URL")
        cached = self.cache.get(url)
        if cached is not MISSING:
            return cached

        headers = {
            "Range": f"bytes=0-{self.max_bytes - 1}",
            # identity: a ranged slice of a gzip stream cannot be decoded
            "Accept-Encoding": "identity",
            "User-Agent": self.user_agent,
        }
        try:
            async with self.throttles.for_host(parsed.hostname).slot():
                async with httpx.AsyncClient(
                    timeout=self.timeout,
                    follow_redirects=True,
                    max_redirects=5,
                    transport=self.transport_factory(),
                    trust_env=False,  # proxies from env would bypass the SSRF guard
                ) as client:
                    async with client.stream("GET", url, headers=headers) as resp:
                        if resp.status_code not in (200, 206):
                            if resp.status_code == 404:
                                raise PreviewError("File not found (HTTP 404)")
                            raise PreviewError(f"Download failed: HTTP {resp.status_code} {resp.reason_phrase}")
                        buf = bytearray()
                        async for chunk in resp.aiter_bytes():
                            buf.extend(chunk)
                            if len(buf) > self.max_bytes:
                                break
                        content = bytes(buf[: self.max_bytes])
                        partial = resp.status_code == 206 or len(buf) > self.max_bytes
                        ctype = resp.headers.get("content-type")
                        final_url = str(resp.url)
        except ThrottleTimeout as e:
            raise PreviewError("Too many downloads queued for this host; try again shortly") from e
        except httpx.TimeoutException as e:
            raise PreviewError("Download timed out") from e
        except httpx.ConnectError as e:
            raise PreviewError(f"Could not connect: {e}") from e
        except httpx.RequestError as e:
            raise PreviewError(f"Download failed: {e}") from e

        result = Fetched(content, partial, ctype.split(";")[0].strip().lower() if ctype else None, final_url)
        self.cache.set(url, result, self.cache_ttl)
        return result


# ---------------------------------------------------------------- decoding


def decode_text(content: bytes, partial: bool = False) -> tuple[str, str]:
    """Decode as UTF-8 (tolerating a cut-off final character), else Windows-1252."""
    try:
        return content.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError as e:
        if partial and e.start >= len(content) - 3:
            try:
                return content[: e.start].decode("utf-8-sig"), "utf-8"
            except UnicodeDecodeError:
                pass
    return content.decode("cp1252", errors="replace"), "windows-1252"


def detect_format(url: str, content_type: str | None, text: str) -> str | None:
    if content_type in CSV_CONTENT_TYPES:
        return "csv"
    if content_type in JSON_CONTENT_TYPES:
        return "json"
    path = urlparse(url).path.lower()
    if path.endswith((".csv", ".tsv", ".txt")):
        return "csv"
    if path.endswith((".json", ".geojson")):
        return "json"
    head = text.lstrip()[:1]
    if head in ("{", "["):
        return "json"
    if head == "<":
        return None  # HTML/XML
    first = text.split("\n", 1)[0]
    if any(d in first for d in (",", ";", "\t", "|")):
        return "csv"
    return None


# --------------------------------------------------------------- inference

_INT = re.compile(r"^[-+]?\d+$")
_FLOAT = re.compile(r"^[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?$")
_FLOAT_COMMA = re.compile(r"^[-+]?\d{1,3}(\.\d{3})*,\d+$|^[-+]?\d+,\d+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")
_DATE_DE = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}( \d{1,2}:\d{2}(:\d{2})?)?$")
_BOOL = {"true", "false", "ja", "nein", "yes", "no", "wahr", "falsch"}


def infer_type(values: list[str]) -> str:
    vals = [v.strip() for v in values if v and v.strip()]
    if not vals:
        return "empty"
    if all(_INT.match(v) for v in vals):
        return "integer"
    if all(_FLOAT.match(v) for v in vals):
        return "number"
    if all(_FLOAT_COMMA.match(v) or _INT.match(v) for v in vals):
        return "number (decimal comma)"
    if all(v.lower() in _BOOL for v in vals):
        return "boolean"
    if all(_DATE.match(v) for v in vals):
        return "date"
    if all(_DATE_DE.match(v) for v in vals):
        return "date (dd.mm.yyyy)"
    return "string"


def json_type(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    return "object"


def _cut(v: Any) -> Any:
    if isinstance(v, str) and len(v) > MAX_CELL_CHARS:
        return v[: MAX_CELL_CHARS - 1] + "…"
    if isinstance(v, (dict, list)):
        s = json.dumps(v, ensure_ascii=False)
        return v if len(s) <= MAX_CELL_CHARS else s[: MAX_CELL_CHARS - 1] + "…"
    return v


# --------------------------------------------------------------------- CSV


def parse_csv(text: str, partial: bool, max_rows: int) -> dict[str, Any]:
    if partial and "\n" in text:
        text = text[: text.rfind("\n")]  # last line is probably cut off
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        counts = {d: sample.split("\n", 1)[0].count(d) for d in ",;\t|"}
        delimiter = max(counts, key=counts.get) if any(counts.values()) else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter, quotechar='"')
    try:
        header = [h.strip() for h in next(reader)]
    except StopIteration as e:
        raise PreviewError("File is empty") from e
    if not any(header):
        raise PreviewError("CSV has no header row")

    rows: list[list[str]] = []
    skipped = 0
    for row in reader:
        if not any(c.strip() for c in row):
            continue
        if len(row) != len(header):
            skipped += 1
            continue
        rows.append(row)
        if len(rows) >= max(max_rows, SAMPLE_ROWS_FOR_TYPES) + 1:
            break
    sample_rows = rows[:SAMPLE_ROWS_FOR_TYPES]
    columns = []
    for i, name in enumerate(header):
        col = [r[i] for r in sample_rows]
        non_empty = [v for v in col if v.strip()]
        columns.append(
            {
                "name": name,
                "type": infer_type(col),
                "empty_in_sample": len(col) - len(non_empty),
                "examples": list(dict.fromkeys(_cut(v.strip()) for v in non_empty))[:3],
            }
        )
    delimiter_name = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}[delimiter]
    return {
        "delimiter": delimiter_name,
        "columns": columns,
        "rows": [[_cut(c) for c in r] for r in rows[:max_rows]],
        "more_rows_available": partial or len(rows) > max_rows,
        "rows_sampled_for_types": len(sample_rows),
        "malformed_rows_skipped": skipped,
    }


# -------------------------------------------------------------------- JSON


def recover_json(text: str) -> Any:
    """Parse JSON, recovering a prefix of a truncated array of objects if needed."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    stripped = text.strip()
    # Truncated top-level array or array inside an object: cut after the last complete "}".
    for end in _candidate_ends(stripped):
        prefix = stripped[: end + 1]
        for suffix in _closers(prefix):
            try:
                return json.loads(prefix + suffix)
            except json.JSONDecodeError:
                continue
    raise PreviewError("Invalid JSON (and could not recover a truncated prefix)")


def _candidate_ends(text: str, tries: int = 20):
    pos = len(text)
    for _ in range(tries):
        pos = text.rfind("}", 0, pos)
        if pos <= 0:
            return
        yield pos


def _closers(prefix: str) -> list[str]:
    """Compute the brackets needed to close `prefix` (ignoring brackets inside strings)."""
    stack: list[str] = []
    in_str = esc = False
    for ch in prefix:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    if in_str:
        return []
    return ["".join(reversed(stack))]


def _records(data: Any) -> tuple[list[dict[str, Any]], str]:
    if isinstance(data, dict) and isinstance(data.get("features"), list):
        feats = [f for f in data["features"] if isinstance(f, dict)]
        rows = []
        for f in feats:
            props = dict(f.get("properties") or {})
            geom = f.get("geometry")
            if isinstance(geom, dict) and geom.get("type"):
                props["geometry_type"] = geom["type"]
            rows.append(props)
        return rows, "geojson_features"
    if isinstance(data, list):
        if data and all(isinstance(x, dict) for x in data[:20]):
            return [x for x in data if isinstance(x, dict)], "array_of_objects"
        return [], "array_of_values"
    if isinstance(data, dict):
        for key in ("data", "results", "items", "records", "rows", "entries", "values", "result"):
            v = data.get(key)
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v[:20]):
                return v, f"objects_under_{key}"
        for key, v in data.items():
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v[:20]):
                return v, f"objects_under_{key}"
        return [], "single_object"
    return [], "scalar"


def parse_json(text: str, partial: bool, max_rows: int) -> dict[str, Any]:
    data = recover_json(text)
    records, structure = _records(data)
    if not records:
        preview = data if structure != "single_object" else {k: _cut(v) for k, v in list(data.items())[:30]}
        return {
            "structure": structure,
            "columns": [],
            "rows": [],
            "note": "JSON is not a list of records; showing the top-level content instead.",
            "content": _cut(preview) if not isinstance(preview, dict) else preview,
        }
    sample = records[:SAMPLE_ROWS_FOR_TYPES]
    types: dict[str, set[str]] = {}
    for rec in sample:
        for k, v in rec.items():
            types.setdefault(k, set()).add(json_type(v))
    columns = []
    for k, ts in types.items():
        non_null = ts - {"null"}
        columns.append({"name": k, "type": non_null.pop() if len(non_null) == 1 else "mixed" if non_null else "null"})
    return {
        "structure": structure,
        "columns": columns,
        "rows": [{k: _cut(v) for k, v in rec.items()} for rec in records[:max_rows]],
        "more_rows_available": partial or len(records) > max_rows,
        "rows_sampled_for_types": len(sample),
    }


def parse(content: bytes, partial: bool, url: str, content_type: str | None, fmt: str | None, max_rows: int):
    text, encoding = decode_text(content, partial)
    fmt = (fmt or "").lower() or detect_format(url, content_type, text)
    if fmt == "geojson":
        fmt = "json"
    if fmt not in ("csv", "json"):
        raise PreviewError(
            f"Cannot preview this file (content type: {content_type or 'unknown'}). "
            "Only CSV and JSON/GeoJSON are supported; pass format='csv' or 'json' to force one."
        )
    max_rows = max(1, min(max_rows, MAX_PREVIEW_ROWS))
    body = parse_csv(text, partial, max_rows) if fmt == "csv" else parse_json(text, partial, max_rows)
    return {"format": fmt, "encoding": encoding, **body}
