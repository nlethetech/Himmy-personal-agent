"""SSRF guard for user-supplied provider ``base_url`` values.

A ``base_url`` is only ever needed for a self-hosted ``openai-compatible`` endpoint
(e.g. the local HimalayaGPT Gemma-4 shim on ``127.0.0.1:8400``). Because the app's
``/provider/test`` endpoint fires a REAL outbound request to whatever URL is supplied,
and the desktop server is reachable cross-origin from the browser, an unvalidated URL
is a Server-Side Request Forgery (SSRF) vector: an attacker could point it at the cloud
metadata service (``169.254.169.254``), at internal services, or use response timing to
port-scan the LAN.

Policy (deliberately strict — this is a *local self-host* convenience, not a proxy):

* Only ``http`` / ``https`` schemes.
* ``https`` to any public host is allowed.
* ``http`` is allowed ONLY to a loopback host (``127.0.0.0/8`` / ``::1`` / ``localhost``)
  — that is the legitimate "I run a model on my own machine" case.
* EVERYTHING ELSE is rejected: link-local (``169.254.0.0/16`` incl. metadata, ``fe80::/10``),
  unique-local IPv6 (``fc00::/7``), and the RFC-1918 private ranges
  (``10/8``, ``172.16/12``, ``192.168/16``) over plain http — these are the SSRF targets.

Raises :class:`BaseUrlError` (a short, user-safe message) on rejection.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


class BaseUrlError(ValueError):
    """A user-facing, value-safe rejection of a provider base URL."""


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for any address an outbound provider call must never be aimed at."""
    return (
        ip.is_link_local       # 169.254.0.0/16 (cloud metadata) + fe80::/10
        or ip.is_private       # 10/8, 172.16/12, 192.168/16, fc00::/7, etc.
        or ip.is_loopback      # 127.0.0.0/8, ::1 (handled separately via loopback http rule)
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _resolved_addresses(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every IP ``host`` resolves to (so a DNS name can't smuggle a blocked IP)."""
    out: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    try:
        for fam, _type, _proto, _canon, sockaddr in socket.getaddrinfo(host, None):
            addr = sockaddr[0]
            try:
                out.append(ipaddress.ip_address(addr))
            except ValueError:
                continue
    except (OSError, UnicodeError):
        # Unresolvable host → treat as not-loopback; the https branch still allows it
        # (the actual request will simply fail later), but http-to-unknown is rejected.
        return []
    return out


def validate_base_url(base_url: str | None) -> str | None:
    """Return a cleaned ``base_url`` or raise :class:`BaseUrlError`.

    ``None``/empty passes through unchanged (the provider doesn't use one).
    """
    if base_url is None:
        return None
    cleaned = base_url.strip()
    if not cleaned:
        return None

    parts = urlsplit(cleaned)
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise BaseUrlError("Endpoint address must start with http:// or https://.")
    host = parts.hostname
    if not host:
        raise BaseUrlError("That endpoint address is missing a host.")

    # Does this host (or any IP it resolves to) sit in loopback? Loopback is the ONLY
    # place plain http is permitted (your own machine).
    is_loopback = host.lower() in ("localhost", "localhost.localdomain")
    literal_ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None

    resolved = [literal_ip] if literal_ip is not None else _resolved_addresses(host)
    if literal_ip is not None and literal_ip.is_loopback:
        is_loopback = True
    elif resolved and all(ip.is_loopback for ip in resolved):
        is_loopback = True

    if scheme == "http":
        if not is_loopback:
            raise BaseUrlError(
                "For safety, a non-local endpoint must use https://. "
                "Plain http:// is only allowed for a server on your own machine "
                "(localhost / 127.0.0.1)."
            )
        return cleaned  # loopback http — the legitimate local self-host case

    # scheme == "https": allow public hosts, but still block https aimed at the
    # metadata service / internal RFC-1918 / link-local targets (SSRF over TLS).
    for ip in resolved:
        if ip.is_loopback:
            continue  # https to your own machine is fine
        if _is_blocked_ip(ip):
            raise BaseUrlError(
                "That endpoint address points at an internal/reserved network and "
                "isn't allowed."
            )
    return cleaned


__all__ = ["BaseUrlError", "validate_base_url"]
