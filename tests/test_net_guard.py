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
        ("::ffff:93.184.215.14", True),
        ("::", False),
        ("255.255.255.255", False),
        ("192.0.2.1", False),  # documentation range
        ("198.18.0.1", False),  # benchmarking range
        ("240.0.0.1", False),  # reserved
        ("2001:db8::1", False),  # documentation range
        ("ff02::1", False),  # multicast
        ("fec0::1", False),  # deprecated site-local
        # IPv6 addresses that carry an IPv4 address are judged by it
        ("64:ff9b::7f00:1", False),  # NAT64 -> 127.0.0.1
        ("64:ff9b::a9fe:a9fe", False),  # NAT64 -> 169.254.169.254
        ("64:ff9b::5db8:d70e", True),  # NAT64 -> 93.184.215.14
        ("::7f00:1", False),  # IPv4-compatible -> 127.0.0.1
        ("::a00:1", False),  # IPv4-compatible -> 10.0.0.1
        ("2002:7f00:1::1", False),  # 6to4 -> 127.0.0.1
        ("2002:c0a8:101::1", False),  # 6to4 -> 192.168.1.1
    ],
)
def test_is_public_ip(ip, expected):
    assert is_public_ip(ip) is expected


@pytest.mark.parametrize("bad", ["", "not-an-ip", "1.2.3", "example.org"])
def test_is_public_ip_rejects_non_addresses(bad):
    with pytest.raises(ValueError):
        is_public_ip(bad)


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
    """host -> one address or a list of addresses."""
    real = asyncio.get_running_loop().getaddrinfo

    async def fake(host, port, *args, **kwargs):
        if host in mapping:
            ips = mapping[host] if isinstance(mapping[host], list) else [mapping[host]]
            return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                    for ip in ips]
        return await real(host, port, *args, **kwargs)

    return fake


def _capture_sent(monkeypatch) -> list[httpx.Request]:
    """Replace the real network send; returns the list the outgoing requests are appended to."""
    seen: list[httpx.Request] = []

    async def fake_send(self, request):
        seen.append(request)
        return httpx.Response(200, content=b"ok", request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_send)
    return seen


async def test_blocks_when_any_resolved_address_is_private(monkeypatch):
    """A name with a public and a private record must not get through on the public one."""
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", _fake_dns({"mixed.example": [PUBLIC_IP, "10.0.0.5"],
                                                         "mixed6.example": [PUBLIC_IP, "fd00::5"]}))
    seen = _capture_sent(monkeypatch)
    async with httpx.AsyncClient(transport=PublicOnlyTransport(), trust_env=False) as client:
        for host in ("mixed.example", "mixed6.example"):
            with pytest.raises(BlockedURLError):
                await client.get(f"https://{host}/x.csv")
    assert not seen


async def test_prefers_ipv4_and_pins_ipv6_literal(monkeypatch):
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", _fake_dns({"dual.example": ["2606:4700::1111", PUBLIC_IP],
                                                         "v6only.example": "2606:4700::1111"}))
    seen = _capture_sent(monkeypatch)
    async with httpx.AsyncClient(transport=PublicOnlyTransport(), trust_env=False) as client:
        await client.get("https://dual.example:8443/a.csv?x=1")
        await client.get("http://v6only.example/b.csv")
    dual, v6 = seen
    assert (dual.url.host, dual.url.port, dual.url.raw_path) == (PUBLIC_IP, 8443, b"/a.csv?x=1")
    assert dual.headers["Host"] == "dual.example:8443" and dual.extensions["sni_hostname"] == "dual.example"
    assert v6.url.host == "2606:4700::1111" and str(v6.url) == "http://[2606:4700::1111]/b.csv"
    assert v6.headers["Host"] == "v6only.example" and "sni_hostname" not in v6.extensions


async def test_unresolvable_host_is_a_connect_error(monkeypatch):
    async def fail(host, port, *args, **kwargs):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fail)
    async with httpx.AsyncClient(transport=PublicOnlyTransport(), trust_env=False) as client:
        with pytest.raises(httpx.ConnectError, match="Could not resolve"):
            await client.get("https://nope.invalid/x.csv")


async def test_each_redirect_hop_is_resolved_and_checked(monkeypatch):
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", _fake_dns({"a.example": PUBLIC_IP, "b.example": "93.184.215.99",
                                                         "c.example": "192.168.7.7"}))
    seen = []

    async def fake_send(self, request):
        seen.append(request)
        target = {"a.example": "https://b.example/2", "b.example": "https://c.example/3"}[request.headers["Host"]]
        return httpx.Response(302, headers={"Location": target}, request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_send)
    async with httpx.AsyncClient(transport=PublicOnlyTransport(), trust_env=False, follow_redirects=True) as client:
        with pytest.raises(BlockedURLError):
            await client.get("https://a.example/1")
    assert [r.url.host for r in seen] == [PUBLIC_IP, "93.184.215.99"]  # c.example was never contacted


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
