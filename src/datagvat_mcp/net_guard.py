"""Outbound request guard for preview downloads (SSRF protection).

The preview tools fetch URLs supplied by MCP clients. Without a guard any
connected agent could make the server request internal addresses (other
containers, the LAN, cloud metadata endpoints) and read the response back.
This transport only lets requests through to publicly routable IP addresses,
checks every redirect hop, and pins the connection to the validated address
so a DNS rebind between check and connect cannot redirect it.
"""

import asyncio
import ipaddress
import socket

import httpx


class BlockedURLError(httpx.RequestError):
    """Raised when a request targets a non-public destination."""


# IPv6 ranges whose last 32 bits are an IPv4 address the packet may end up at:
# NAT64 (RFC 6052) and the deprecated IPv4-compatible addresses.
_EMBEDS_IPV4 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("::/96"))


def is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%", 1)[0])
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        elif any(addr in net for net in _EMBEDS_IPV4):
            addr = ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)  # judge by the embedded address
    # is_global still passes the deprecated site-local range fec0::/10, which networks may use internally.
    return addr.is_global and not addr.is_multicast and not getattr(addr, "is_site_local", False)


class PublicOnlyTransport(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.scheme not in ("http", "https"):
            raise BlockedURLError(f"URL scheme '{url.scheme}' is not allowed", request=request)

        host = url.host
        port = url.port or (443 if url.scheme == "https" else 80)
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as e:
            raise httpx.ConnectError(f"Could not resolve host '{host}'", request=request) from e

        ips = sorted({info[4][0] for info in infos}, key=lambda ip: (":" in ip, ip))  # IPv4 first
        if not ips or not all(is_public_ip(ip) for ip in ips):
            raise BlockedURLError(
                f"Host '{host}' resolves to a private or reserved address; request blocked",
                request=request,
            )

        # Connect to the validated address. The Host header is already set from
        # the original URL, and sni_hostname keeps TLS verification on the name.
        request.url = url.copy_with(host=ips[0])
        if url.scheme == "https":
            request.extensions = {**request.extensions, "sni_hostname": host}
        return await super().handle_async_request(request)
