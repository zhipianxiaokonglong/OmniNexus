"""Egress policy for user-supplied upstream URLs.

User-provided Base URLs are network input, not just strings: a request that
also carries a stored API key must not be able to aim that key at an arbitrary
host. Two defences live here:

* syntactic IP-literal blocking (works without DNS, so it also applies when a
  test transport is injected);
* DNS resolution with pinned connection address, so a later DNS answer cannot
  change where the request actually goes (DNS rebinding).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

from omninexus.providers.urls import BaseUrlError, validate_base_url

_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


class EgressError(BaseUrlError):
    """Target address is not allowed by the outbound network policy."""


def parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return the address literal, or None when ``value`` is a hostname."""
    text = (value or "").strip().strip("[]")
    if not text:
        return None
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def is_blocked(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for addresses that must not be reachable through user input.

    ``is_global`` covers loopback, RFC1918/ULA private space, link-local
    (including the 169.254.169.254 metadata address), multicast, reserved and
    unspecified addresses such as 0.0.0.0 and ::.
    """
    return not address.is_global


def check_host_syntax(host: str | None) -> None:
    """Reject obvious local targets without touching DNS.

    Always applied. ``allow_private_targets`` does not bypass this check; it is
    the caller that decides whether to skip this function entirely.
    """
    name = (host or "").strip().lower()
    if not name:
        raise EgressError("Base URL 缺少主机名")
    if name in _LOOPBACK_NAMES:
        raise EgressError(
            "不允许访问本机地址；本地模型请在 config 中启用对应内置提供方，"
            "或显式开启 server.allow_private_targets"
        )
    address = parse_ip(name)
    if address is not None and is_blocked(address):
        raise EgressError(
            "不允许访问回环/私网/链路本地地址；如确需连接本地模型，"
            "请显式开启 server.allow_private_targets"
        )


@dataclass(frozen=True, slots=True)
class PinnedTarget:
    """A target whose connection address has already been validated."""

    url: str
    host_header: str
    server_hostname: str | None

    @property
    def extensions(self) -> dict[str, str]:
        if not self.server_hostname:
            return {}
        # httpcore honours this for the TLS handshake, so the certificate is
        # still validated against the hostname while connecting to the IP.
        return {"sni_hostname": self.server_hostname}


def _with_host(url: str, host: str) -> str:
    parsed = urlparse(url)
    netloc = f"[{host}]" if ":" in host else host
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


def pin_to_address(url: str, address: str) -> PinnedTarget:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return PinnedTarget(
        url=_with_host(url, address),
        host_header=parsed.netloc,
        server_hostname=host if parsed.scheme == "https" else None,
    )


async def resolve_addresses(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise EgressError(f"无法解析主机名：{host}") from exc
    seen: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in seen:
            seen.append(address)
    return seen


async def guard_target(
    base_url: str,
    *,
    allow_private: bool,
    resolve: bool,
) -> PinnedTarget | None:
    """Validate an outbound target and return its pinned address.

    Returns None when pinning is not possible (a test transport is injected and
    no DNS lookup happened); the caller then sends the request unchanged.
    """
    url = validate_base_url(base_url)
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if allow_private:
        return None
    check_host_syntax(host)
    if not resolve:
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = await resolve_addresses(host, port)
    if not addresses:
        raise EgressError(f"无法解析主机名：{host}")
    for item in addresses:
        address = parse_ip(item)
        if address is not None and is_blocked(address):
            raise EgressError(
                "目标主机解析到回环/私网/链路本地地址；如确需连接本地模型，"
                "请显式开启 server.allow_private_targets"
            )
    return pin_to_address(url, addresses[0])
