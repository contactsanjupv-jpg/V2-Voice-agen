"""
SSRF-safe outbound fetcher for the website importer. Website URLs are
untrusted input by definition (spec §8) — this module is the only place
allowed to make an outbound request to a customer-supplied URL.

Defenses:
  1. Scheme allowlist (http/https only)
  2. DNS pre-resolution + IP-literal validation BEFORE connecting, rejecting
     private/loopback/link-local/multicast/reserved ranges and common
     cloud-metadata addresses. If ANY resolved address for a hostname is
     private/reserved, the whole hostname is rejected — a multi-answer
     DNS response mixing public and private IPs is treated as a red flag,
     not something we try to selectively trust.
  3. The actual request goes to the real hostname (not a substituted IP
     literal) — this keeps TLS SNI and certificate-hostname validation
     correct for real HTTPS sites. An earlier version of this fetcher
     pinned the connection to the validated IP by rewriting it into the
     URL string; that broke on IPv6 hosts (colons inside a URL need
     bracketing or they're parsed as a port) and would have broken HTTPS
     certificate validation for most real sites even once that was fixed
     (a cert for "example.com" doesn't validate against a request to its
     bare IP). Known, accepted tradeoff from resolving by hostname again
     at connect time: a narrow DNS-rebinding window between our
     pre-validation and httpx's own resolution — an attacker would need
     to flip DNS for the target hostname within the few-hundred-millisecond
     gap between the two lookups. Closing that fully needs a custom
     transport with real connection-level IP pinning + correct SNI
     override; worth doing if this importer becomes a bigger attack
     surface, not something skipped by oversight.
  4. Redirects followed manually, one hop at a time, re-validating the
     target host at every hop (a public URL is not allowed to redirect
     to a private one)
  5. Size limit, timeout, redirect-count limit, content-type allowlist
"""
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

MAX_REDIRECTS = 5
MAX_BYTES = 5 * 1024 * 1024  # 5 MB
TIMEOUT_SECONDS = 10.0
ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml")

_BLOCKED_LITERAL_HOSTS = {"169.254.169.254", "metadata.google.internal"}


class SSRFBlockedError(Exception):
    pass


class FetchTooLargeError(Exception):
    pass


@dataclass
class SafeFetchResult:
    final_url: str
    status_code: int
    content_type: str
    text: str


_NAT64_WELL_KNOWN_PREFIX = ipaddress.ip_network("64:ff9b::/96")


def _is_private_or_reserved(ip_str: str) -> bool:
    ip = ipaddress.ip_address(ip_str)

    # NAT64 well-known prefix (RFC 6052): networks using IPv6-only
    # connectivity with automatic IPv4 translation synthesize addresses
    # like 64:ff9b::<embedded-ipv4>. Python's ipaddress module correctly
    # flags the whole prefix as reserved, but the actual destination is
    # whatever IPv4 is embedded in the last 32 bits — unpack it and
    # validate the real target instead of the wrapper.
    if isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64_WELL_KNOWN_PREFIX:
        embedded_ipv4 = ipaddress.IPv4Address(ip.packed[12:16])
        return _is_private_or_reserved(str(embedded_ipv4))

    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _validate_host_resolves_safely(host: str) -> None:
    """Raises SSRFBlockedError if `host` is a blocked literal or resolves
    to any private/reserved address. Does not return an IP — the caller
    connects by hostname (see module docstring for why)."""
    if host in _BLOCKED_LITERAL_HOSTS:
        raise SSRFBlockedError(f"Blocked host: {host}")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise SSRFBlockedError(f"DNS resolution failed for {host}") from e

    resolved_ips = {info[4][0] for info in infos}
    if not resolved_ips:
        raise SSRFBlockedError(f"No addresses resolved for {host}")
    for ip_str in resolved_ips:
        if _is_private_or_reserved(ip_str):
            raise SSRFBlockedError(f"{host} resolves to a private/reserved address ({ip_str})")


def _validate_url(url: str) -> str:
    """Returns the validated hostname, or raises SSRFBlockedError."""
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise SSRFBlockedError(f"Disallowed scheme: {parsed.scheme}")
    if not parsed.hostname:
        raise SSRFBlockedError("URL has no hostname")
    if parsed.port not in (None, 80, 443):
        raise SSRFBlockedError(f"Disallowed port: {parsed.port}")
    _validate_host_resolves_safely(parsed.hostname)
    return parsed.hostname


def safe_fetch(url: str) -> SafeFetchResult:
    """
    Fetches `url` following redirects manually (re-validated at each hop),
    enforcing size/time/content-type limits. Raises SSRFBlockedError or
    FetchTooLargeError on any violation — callers must not swallow these
    silently and fall back to treating the content as safe.
    """
    current_url = url
    for _ in range(MAX_REDIRECTS + 1):
        _validate_url(current_url)  # raises if unsafe

        with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
            try:
                resp = client.get(
                    current_url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                        ),
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.9",
                    },
                )
            except httpx.TimeoutException as e:
                raise SSRFBlockedError(f"Timed out connecting to {current_url}") from e
            except httpx.ConnectError as e:
                raise SSRFBlockedError(f"Could not connect to {current_url}") from e
            except httpx.RequestError as e:
                raise SSRFBlockedError(f"Could not fetch {current_url}: {e}") from e

        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location")
            if not location:
                raise SSRFBlockedError("Redirect with no Location header")
            current_url = str(httpx.URL(current_url).join(location))
            continue

        content_type = resp.headers.get("content-type", "").split(";")[0].strip()
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise SSRFBlockedError(f"Disallowed content-type: {content_type}")

        content_length = resp.headers.get("content-length")
        if content_length and int(content_length) > MAX_BYTES:
            raise FetchTooLargeError(f"Response too large: {content_length} bytes")
        body = resp.content
        if len(body) > MAX_BYTES:
            raise FetchTooLargeError(f"Response body exceeded {MAX_BYTES} bytes")

        return SafeFetchResult(
            final_url=current_url,
            status_code=resp.status_code,
            content_type=content_type,
            text=body.decode(resp.encoding or "utf-8", errors="replace"),
        )

    raise SSRFBlockedError("Too many redirects")