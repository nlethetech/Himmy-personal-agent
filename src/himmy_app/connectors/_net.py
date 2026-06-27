"""Secure, resilient shared HTTP + JSON-snapshot helpers for connectors.

Every connector (Daraz, Foodmandu, Buddha Air, Bussewa) and ``do_concierge`` talk to
third-party Nepal storefront/booking endpoints that are *open* (no auth) but *hostile* in
the ways open endpoints tend to be: they 302 to captcha/login walls, serve HTML error pages
with a 200, occasionally return huge bodies, and rate-limit. This module is the one place we
get that right so individual connectors stay small and can't reintroduce the same bugs.

What it guarantees for an outbound request:

* **SSRF defence** — the destination host is DNS-resolved and *every* resolved address is
  checked; private / loopback / link-local / reserved IP space is refused, as are non-http(s)
  schemes. When ``allow_hosts`` is supplied the final host must be on that allow-list. This
  protects against a malicious redirect (or a DNS answer) pointing us at internal services.
* **No blind redirects** — redirects are NOT auto-followed. A single manual hop is allowed,
  but only after the ``Location`` target passes the same SSRF + allow-list checks. This is the
  fix for the Daraz class of bug where ``follow_redirects=True`` silently chased a captcha 302
  and surfaced as an opaque ``JSONDecodeError`` from ``.json()`` on an HTML body.
* **Content-Type guard** — the response must look like JSON (``application/json``, a ``+json``
  suffix, or ``text/json``). A captcha/HTML body raises :class:`NetError` and never reaches
  ``.json()``.
* **Size cap** — the body is streamed and aborted once it exceeds ``max_bytes`` so a hostile
  or runaway endpoint can't exhaust memory.
* **Status + bounded retry** — non-2xx raises. We retry *only* on 429 and transient 5xx
  (500/502/503/504), at most ``retries`` times, honouring ``Retry-After`` when present and
  otherwise using a short jittered backoff. Other 4xx are never retried.
* **Quiet on failure** — response bodies and secrets are never logged; errors carry a short,
  body-free message.

Snapshot helpers (``read_json_snapshot`` / ``write_json_snapshot``) give connectors a crash-safe
on-disk cache so a transient upstream outage degrades to slightly-stale data instead of an error.
Writes go through :func:`atomic_write_text` (temp file in the same directory + ``os.replace``),
which is atomic on POSIX and avoids leaving a half-written cache file behind on a crash.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import random
import socket
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

__all__ = [
    "NetError",
    "safe_get_json",
    "atomic_write_text",
    "read_json_snapshot",
    "write_json_snapshot",
]


class NetError(Exception):
    """Raised when a guarded request cannot be completed safely or successfully.

    Messages are intentionally short and never contain response bodies or secrets.
    """


# Statuses we will retry (rate-limit + transient server errors). Everything else is terminal.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
# JSON-ish content types we accept. Anything else (notably text/html) is rejected.
_JSON_CT_EXACT = frozenset({"application/json", "text/json", "application/jsonrequest"})


def _is_blocked_ip(ip: str) -> bool:
    """Return True for any address we must never connect to (SSRF surface)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        # Not a literal IP we can reason about -> treat as unsafe.
        return True
    # Covers loopback (127/8, ::1), link-local (169.254/16, fe80::/10),
    # private (10/8, 172.16/12, 192.168/16, fc00::/7), reserved/unspecified/multicast.
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    ):
        return True
    # IPv4-mapped / -compatible IPv6 (e.g. ::ffff:127.0.0.1) — unwrap and re-check.
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None and _is_blocked_ip(str(mapped)):
        return True
    sixto4 = getattr(addr, "sixtofour", None)
    if sixto4 is not None and _is_blocked_ip(str(sixto4)):
        return True
    return False


def _resolve_addresses(host: str) -> list[str]:
    """Resolve ``host`` to its IP string(s). Raises NetError if it cannot be resolved."""
    if not host:
        raise NetError("missing host")
    # If the host is already an IP literal, getaddrinfo still works and normalises it.
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise NetError(f"cannot resolve host: {host!r} ({exc.__class__.__name__})") from None
    addrs = {info[4][0] for info in infos}
    if not addrs:
        raise NetError(f"cannot resolve host: {host!r}")
    return sorted(addrs)


def _validate_url(url: str, allow_hosts: frozenset[str] | None) -> str:
    """Validate scheme, allow-list and SSRF safety for ``url``; return the host.

    Raises NetError on any violation. Performs DNS resolution and refuses if *any* resolved
    address falls in private/loopback/link-local/reserved space.
    """
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise NetError(f"refusing non-http(s) scheme: {scheme!r}")
    host = (parts.hostname or "").lower()
    if not host:
        raise NetError("URL has no host")
    if allow_hosts is not None and host not in allow_hosts:
        raise NetError(f"host not in allow-list: {host!r}")
    for ip in _resolve_addresses(host):
        if _is_blocked_ip(ip):
            raise NetError(f"refusing private/reserved address for host {host!r}")
    return host


def _normalise_allow_hosts(allow_hosts: list[str] | set[str] | tuple[str, ...] | None) -> (
    frozenset[str] | None
):
    if allow_hosts is None:
        return None
    return frozenset(h.strip().lower() for h in allow_hosts if h and h.strip())


def _is_json_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    main = content_type.split(";", 1)[0].strip().lower()
    if main in _JSON_CT_EXACT:
        return True
    # +json structured-suffix (e.g. application/vnd.api+json).
    return main.endswith("+json")


async def _read_capped(response: httpx.Response, max_bytes: int) -> bytes:
    """Stream the body, aborting (NetError) if it exceeds ``max_bytes``."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise NetError(f"response exceeds size cap ({max_bytes} bytes)")
        chunks.append(chunk)
    return b"".join(chunks)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a ``Retry-After`` header (delta-seconds form) into a bounded float, else None."""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        secs = float(raw.strip())
    except (TypeError, ValueError):
        # HTTP-date form is rare for these endpoints; fall back to jittered backoff.
        return None
    if secs < 0:
        return None
    return min(secs, 30.0)  # never sleep absurdly long on a hostile header


async def _send_once(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None,
    headers: dict[str, str] | None,
    allow_hosts: frozenset[str] | None,
    max_bytes: int,
) -> Any:
    """One request (with at most one manual, validated redirect hop). Returns parsed JSON.

    Raises NetError on guard violations / bad content-type / oversize bodies. Re-raises
    httpx.HTTPStatusError so the caller's retry logic can inspect the status code.
    """
    current_url = url
    current_params = params
    for hop in range(2):  # original request + at most one manual redirect
        _validate_url(current_url, allow_hosts)
        request = client.build_request("GET", current_url, params=current_params, headers=headers)
        response = await client.send(request, stream=True)
        try:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location or hop == 1:
                    raise NetError("unexpected/looping redirect")
                # Resolve relative redirects against the current URL, then re-validate.
                current_url = str(response.url.join(location))
                current_params = None  # params already encoded into the original URL
                continue
            response.raise_for_status()
            if not _is_json_content_type(response.headers.get("content-type")):
                raise NetError(
                    "non-JSON response "
                    f"(content-type={response.headers.get('content-type', '?')!r})"
                )
            body = await _read_capped(response, max_bytes)
        finally:
            await response.aclose()
        try:
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise NetError(f"invalid JSON body ({exc.__class__.__name__})") from None
    raise NetError("too many redirects")


async def safe_get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    allow_hosts: list[str] | set[str] | tuple[str, ...] | None = None,
    allow_any: bool = False,
    timeout: float = 20.0,
    max_bytes: int = 5_000_000,
    retries: int = 1,
) -> Any:
    """GET ``url`` and return parsed JSON, with SSRF / redirect / content / size / retry guards.

    Args:
        url: Absolute http(s) URL.
        params: Optional query parameters.
        headers: Optional request headers (e.g. a browser User-Agent). Never logged.
        allow_hosts: The request host AND any redirect target host must be in this allow-list.
            It is **mandatory**: the per-IP SSRF check is advisory only (httpx re-resolves DNS at
            connect time, so a DNS-rebinding host can pass validation then connect to a private IP),
            which makes the fixed allow-list the real enforcing control. Calling with no
            ``allow_hosts`` and no ``allow_any=True`` raises :class:`NetError`.
        allow_any: Explicit opt-out of the allow-list (host is then only IP/SSRF-checked). Only set
            this for a genuinely dynamic, caller-vetted URL — never with attacker-influenced input.
        timeout: Per-attempt timeout in seconds.
        max_bytes: Hard cap on response body size; larger bodies raise NetError.
        retries: Extra attempts (beyond the first) on 429 / transient 5xx only.

    Returns:
        The decoded JSON value (dict, list, etc.).

    Raises:
        NetError: on any guard violation, non-JSON body, oversize body, or after exhausting
            retries on a retryable status, or on a non-retryable HTTP error / network failure.
    """
    hosts = _normalise_allow_hosts(allow_hosts)
    if not hosts and not allow_any:
        # Mandatory allow-list: refuse a wide-open fetch rather than rely on the advisory per-IP
        # check alone (which a DNS-rebinding host can defeat). Pass allow_any=True to opt out.
        raise NetError("allow_hosts is required (or pass allow_any=True for a vetted dynamic URL)")
    attempts = max(0, int(retries)) + 1
    last_exc: BaseException | None = None
    # follow_redirects=False is the load-bearing flag: we validate every hop ourselves.
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, trust_env=False
    ) as client:
        for attempt in range(attempts):
            try:
                return await _send_once(
                    client,
                    url,
                    params=params,
                    headers=headers,
                    allow_hosts=hosts,
                    max_bytes=max_bytes,
                )
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status not in _RETRYABLE_STATUS or attempt == attempts - 1:
                    raise NetError(f"HTTP {status} for request") from None
                delay = _retry_after_seconds(exc.response)
                last_exc = exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                # Network-level transient failures are retried like 5xx.
                if attempt == attempts - 1:
                    raise NetError(f"network error ({exc.__class__.__name__})") from None
                delay = None
                last_exc = exc
            # Backoff before the next attempt: honour Retry-After else short jittered backoff.
            if delay is None:
                delay = min(0.3 * (2 ** attempt), 5.0) + random.uniform(0.0, 0.25)
            await asyncio.sleep(delay)
    # Defensive: loop always returns or raises above.
    raise NetError(f"request failed after {attempts} attempt(s)") from last_exc


def atomic_write_text(path: str | os.PathLike[str], text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file in the same dir + os.replace).

    The temp file is created in the destination directory so ``os.replace`` is a same-filesystem
    atomic rename. On any failure the temp file is cleaned up and the original (if any) is left
    untouched, so a crash mid-write never yields a half-written file.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_json_snapshot(path: str | os.PathLike[str], max_age_s: float) -> Any | None:
    """Return the JSON object cached at ``path`` if it is fresh, else None.

    Returns None when the file is missing, older than ``max_age_s`` seconds, or unreadable /
    corrupt — callers treat None as a cache miss and refetch.
    """
    target = Path(path)
    try:
        mtime = target.stat().st_mtime
    except OSError:
        return None
    if max_age_s >= 0 and (time.time() - mtime) > max_age_s:
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def write_json_snapshot(path: str | os.PathLike[str], obj: Any) -> None:
    """Serialise ``obj`` to JSON and write it crash-safely via :func:`atomic_write_text`."""
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
