"""
Real, meaningful SSRF tests (spec §53: "test malicious redirects"). These
don't need network access — they test the IP-classification and
validation logic directly, which is where the actual security decision
gets made.
"""
import pytest

from app.core.ssrf_safe_fetch import SSRFBlockedError, _is_private_or_reserved, _validate_host_resolves_safely


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "10.0.0.1",  # RFC1918 private
        "172.16.0.1",  # RFC1918 private
        "192.168.1.1",  # RFC1918 private
        "169.254.169.254",  # cloud metadata / link-local
        "0.0.0.0",  # unspecified
        "224.0.0.1",  # multicast
        "::1",  # IPv6 loopback
        "fe80::1",  # IPv6 link-local
        "fc00::1",  # IPv6 unique local (private)
    ],
)
def test_blocks_private_and_reserved_ips(ip):
    assert _is_private_or_reserved(ip) is True


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"])
def test_allows_public_ips(ip):
    assert _is_private_or_reserved(ip) is False


def test_blocks_known_cloud_metadata_hostname():
    with pytest.raises(SSRFBlockedError):
        _validate_host_resolves_safely("metadata.google.internal")


def test_blocks_literal_metadata_ip_as_hostname(monkeypatch):
    """A hostname that resolves ONLY to the metadata IP must be blocked
    even without being in the literal-host blocklist by name."""
    import socket

    def fake_getaddrinfo(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(SSRFBlockedError):
        _validate_host_resolves_safely("attacker-controlled-dns.example.com")


def test_blocks_dns_rebind_to_private_ip(monkeypatch):
    """Even if ONE of several A records resolved is private, the whole
    hostname is rejected — a multi-answer response mixing public and
    private IPs is a red flag we don't try to selectively trust."""
    import socket

    def fake_getaddrinfo(host, port):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(SSRFBlockedError):
        _validate_host_resolves_safely("mixed-answers.example.com")


def test_allows_clean_public_resolution(monkeypatch):
    import socket

    def fake_getaddrinfo(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    _validate_host_resolves_safely("example.com")


def test_allows_clean_ipv6_only_resolution(monkeypatch):
    """The exact case that broke before the fix: a host that only
    resolves to a public IPv6 address must be accepted, not crash."""
    import socket

    def fake_getaddrinfo(host, port):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", 0, 0, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    _validate_host_resolves_safely("ipv6-only.example.com")


@pytest.mark.parametrize(
    "nat64_address,embeds_private,description",
    [
        ("64:ff9b::c1d:81fc", False, "embeds a public IPv4 (12.29.129.252) — must be ALLOWED"),
        ("64:ff9b::a00:1", True, "embeds a private IPv4 (10.0.0.1) — must still be BLOCKED"),
    ],
)
def test_nat64_well_known_prefix_unpacks_embedded_ipv4(nat64_address, embeds_private, description):
    """
    Real bug found in production use: networks using IPv6-only
    connectivity with NAT64/DNS64 synthesize addresses under the
    64:ff9b::/96 well-known prefix (RFC 6052). Python's ipaddress module
    flags the whole prefix as 'reserved', which false-positived on
    completely ordinary public websites. The fix unpacks the embedded
    IPv4 and validates THAT — this test locks in both directions: the
    false positive is gone, and wrapping a private address in a NAT64
    prefix doesn't become a way to sneak past the SSRF check.
    """
    assert _is_private_or_reserved(nat64_address) is embeds_private, description