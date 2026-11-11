"""Tests for the SSRF guard and the patched fetch_preview_bytes().

Run (from the repo root):
  docker build --target build -t datagvat-mcp:build .
  docker run --rm -e PYTHONPATH=/install/lib/python3.12/site-packages \
    -v ./tests:/src/mcp/tests_container:ro datagvat-mcp:build \
    sh -c 'pip install -q pytest==9.0.2 pytest-asyncio==1.3.0 && python -m pytest -q -p no:cacheprovider tests_container'
"""

import asyncio
import socket

import httpx
import pytest

from app import preview
from app.net_guard import BlockedURLError, PublicOnlyTransport, is_public_ip
from app.preview import PreviewError, fetch_preview_bytes

PUBLIC_IP = "93.184.215.14"


@pytest.mark.parametrize(
    "ip,expected",
    [
        ("93.184.215.14", True),
        ("2606:4700::1111", True),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("172.17.0.1", False),
        ("192.168.1.1", False),
        ("100.64.0.1", False),
        ("169.254.169.254", False),
        ("0.0.0.0", False),
        ("224.0.0.1", False),
        ("::1", False),
        ("fd00::1", False),
        ("fe80::1%eth0", False),
        ("::ffff:127.0.0.1", False),
        ("::ffff:10.0.0.1", False),
    ],
)
def test_is_public_ip(ip, expected):
    assert is_public_ip(ip) is expected


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.1/data.csv",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]:8000/",
        "http://[::ffff:127.0.0.1]/",
        "http://2130706433/",
        "http://localhost/",
    ],
)
async def test_transport_blocks_private_destinations(url):
    async with httpx.AsyncClient(transport=PublicOnlyTransport(), trust_env=False) as client:
        with pytest.raises(BlockedURLError):
            await client.get(url)


def _fake_dns(mapping):
    real = asyncio.get_running_loop().getaddrinfo

    async def fake(host, port, *args, **kwargs):
        if host in mapping:
            fam = socket.AF_INET6 if ":" in mapping[host] else socket.AF_INET
            return [(fam, socket.SOCK_STREAM, 6, "", (mapping[host], port))]
        return await real(host, port, *args, **kwargs)

    return fake


async def test_transport_blocks_host_resolving_to_private(monkeypatch):
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", _fake_dns({"evil.example": "192.168.0.10"}))
    async with httpx.AsyncClient(transport=PublicOnlyTransport(), trust_env=False) as client:
        with pytest.raises(BlockedURLError):
            await client.get("http://evil.example/x.csv")


async def test_transport_blocks_redirect_to_private_and_pins_ip(monkeypatch):
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", _fake_dns({"public.example": PUBLIC_IP}))
    seen = []

    async def fake_send(self, request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "http://127.0.0.1:8000/health"}, request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_send)
    async with httpx.AsyncClient(transport=PublicOnlyTransport(), trust_env=False, follow_redirects=True) as client:
        with pytest.raises(BlockedURLError):
            await client.get("https://public.example/data.csv")

    assert len(seen) == 1  # the redirect target was never contacted
    req = seen[0]
    assert req.url.host == PUBLIC_IP
    assert req.headers["Host"] == "public.example"
    assert req.extensions["sni_hostname"] == "public.example"


async def test_transport_rejects_non_http_scheme():
    t = PublicOnlyTransport()
    with pytest.raises(BlockedURLError):
        await t.handle_async_request(httpx.Request("GET", "ftp://example.com/file.csv"))


# --- fetch_preview_bytes with a mocked transport -----------------------------


def _use_mock(monkeypatch, handler):
    monkeypatch.setattr(preview, "PublicOnlyTransport", lambda: httpx.MockTransport(handler))


async def test_fetch_partial_content_sends_range(monkeypatch):
    captured = {}

    def handler(request):
        captured["range"] = request.headers.get("Range")
        return httpx.Response(206, content=b"partial content")

    _use_mock(monkeypatch, handler)
    content, partial = await fetch_preview_bytes("http://example.com/data.csv", max_bytes=1000)
    assert (content, partial) == (b"partial content", True)
    assert captured["range"] == "bytes=0-999"


async def test_fetch_full_content_truncated(monkeypatch):
    _use_mock(monkeypatch, lambda r: httpx.Response(200, content=b"full content here"))
    content, partial = await fetch_preview_bytes("http://example.com/data.csv", max_bytes=10)
    assert (content, partial) == (b"full conte", True)


async def test_fetch_small_file_not_partial(monkeypatch):
    _use_mock(monkeypatch, lambda r: httpx.Response(200, content=b"a,b\n1,2\n"))
    content, partial = await fetch_preview_bytes("http://example.com/data.csv", max_bytes=1000)
    assert (content, partial) == (b"a,b\n1,2\n", False)


async def test_fetch_stops_reading_large_body(monkeypatch):
    produced = 0

    async def body():
        nonlocal produced
        for _ in range(10_000):  # would be ~640 MB if fully read
            produced += 1
            yield b"x" * 65536

    _use_mock(monkeypatch, lambda r: httpx.Response(200, content=body()))
    content, partial = await fetch_preview_bytes("http://example.com/big.csv", max_bytes=100_000)
    assert len(content) == 100_000 and partial is True
    assert produced <= 3


async def test_fetch_caps_oversized_206(monkeypatch):
    _use_mock(monkeypatch, lambda r: httpx.Response(206, content=b"y" * 5000))
    content, partial = await fetch_preview_bytes("http://example.com/data.csv", max_bytes=100)
    assert len(content) == 100 and partial is True


async def test_fetch_not_found(monkeypatch):
    _use_mock(monkeypatch, lambda r: httpx.Response(404))
    with pytest.raises(PreviewError, match="not found") as e:
        await fetch_preview_bytes("http://example.com/missing.csv")
    assert e.value.reason == "fetch_failed"


async def test_fetch_other_status(monkeypatch):
    _use_mock(monkeypatch, lambda r: httpx.Response(500))
    with pytest.raises(PreviewError, match="HTTP error 500"):
        await fetch_preview_bytes("http://example.com/data.csv")


@pytest.mark.parametrize(
    "exc,msg",
    [(httpx.ReadTimeout, "timed out"), (httpx.ConnectError, "Could not connect")],
)
async def test_fetch_network_errors(monkeypatch, exc, msg):
    def handler(request):
        raise exc("boom", request=request)

    _use_mock(monkeypatch, handler)
    with pytest.raises(PreviewError, match=msg):
        await fetch_preview_bytes("http://example.com/data.csv")


async def test_fetch_blocked_url_maps_to_preview_error():
    with pytest.raises(PreviewError, match="private or reserved"):
        await fetch_preview_bytes("http://127.0.0.1:8000/health")
