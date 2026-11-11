"""SSRF guard used for preview downloads."""

import asyncio
import socket

import httpx
import pytest

from datagvat_mcp.net_guard import BlockedURLError, PublicOnlyTransport, is_public_ip

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
async def test_blocks_private_destinations(url):
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


async def test_blocks_hostname_resolving_to_private(monkeypatch):
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", _fake_dns({"evil.example": "192.168.0.10"}))
    async with httpx.AsyncClient(transport=PublicOnlyTransport(), trust_env=False) as client:
        with pytest.raises(BlockedURLError):
            await client.get("http://evil.example/x.csv")


async def test_blocks_redirect_to_private_and_pins_ip(monkeypatch):
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


async def test_rejects_non_http_scheme():
    with pytest.raises(BlockedURLError):
        await PublicOnlyTransport().handle_async_request(httpx.Request("GET", "ftp://example.com/f.csv"))
