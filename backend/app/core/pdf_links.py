"""A PDF link Claude hands us is fetched from inside the api container, so it must not reach the container's own
network (D217): only http(s), and every address the host resolves to must be public — checked before the first
request and again on every redirect hop."""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

from app.core.errors import InvalidInput


async def resolve_host(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_V4_COMPATIBLE = ipaddress.ip_network("::/96")


def _public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _public(str(ip.ipv4_mapped))
        if ip in _NAT64:
            return _public(str(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)))
        if ip in _V4_COMPATIBLE or ip.is_site_local:
            return False
    return ip.is_global and not ip.is_multicast


async def check_url(url: str) -> None:
    """Raises InvalidInput("unsafe_url") for a non-http(s) URL or a host with any non-public address."""
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError for a port past 65535
    except ValueError:
        raise InvalidInput("unsafe_url", url=url) from None
    host = parts.hostname
    if parts.scheme not in ("http", "https") or not host:
        raise InvalidInput("unsafe_url", url=url)
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addresses = await resolve_host(host)
        except OSError:
            raise InvalidInput("unsafe_url", url=url) from None
    # ponytail: checked at request time, not pinned to the connection; a DNS answer that changes between this check
    # and the connect (rebinding) slips through. Pin the address in a custom transport if that ever matters.
    if not addresses or not all(map(_public, addresses)):
        raise InvalidInput("unsafe_url", url=url)


async def guard(request: httpx.Request) -> None:
    """httpx request hook: runs before the first request and before each redirect hop."""
    await check_url(str(request.url))
