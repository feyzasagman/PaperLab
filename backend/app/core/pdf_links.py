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


def _public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return ip.is_global and not ip.is_multicast


async def check_url(url: str) -> None:
    """Raises InvalidInput("unsafe_url") for a non-http(s) URL or a host with any non-public address."""
    parts = urlsplit(url)
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
