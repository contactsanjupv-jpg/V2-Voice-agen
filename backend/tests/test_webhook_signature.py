import hashlib
import hmac
import time

import pytest

from app.webhooks.retell_signature import InvalidRetellSignature, verify_retell_signature

API_KEY = "test-retell-api-key"


def _sign(body: bytes, timestamp_ms: int, key: str = API_KEY) -> str:
    digest = hmac.new(key.encode(), body + str(timestamp_ms).encode(), hashlib.sha256).hexdigest()
    return f"v={timestamp_ms},d={digest}"


def test_valid_signature_passes():
    body = b'{"event":"call_ended","call":{"call_id":"abc123"}}'
    header = _sign(body, int(time.time() * 1000))
    verify_retell_signature(body, header, API_KEY)  # should not raise


def test_missing_header_rejected():
    with pytest.raises(InvalidRetellSignature):
        verify_retell_signature(b"{}", None, API_KEY)


def test_malformed_header_rejected():
    with pytest.raises(InvalidRetellSignature):
        verify_retell_signature(b"{}", "not-a-valid-signature", API_KEY)


def test_wrong_key_rejected():
    body = b'{"event":"call_ended"}'
    header = _sign(body, int(time.time() * 1000), key="a-different-key")
    with pytest.raises(InvalidRetellSignature):
        verify_retell_signature(body, header, API_KEY)


def test_tampered_body_rejected():
    body = b'{"event":"call_ended","call":{"call_id":"abc123"}}'
    header = _sign(body, int(time.time() * 1000))
    tampered_body = b'{"event":"call_ended","call":{"call_id":"XXXXXX"}}'
    with pytest.raises(InvalidRetellSignature):
        verify_retell_signature(tampered_body, header, API_KEY)


def test_replayed_old_timestamp_rejected():
    """A signature that was valid 10 minutes ago must be rejected now —
    this is the replay-attack guard."""
    body = b'{"event":"call_ended"}'
    ten_minutes_ago_ms = int((time.time() - 600) * 1000)
    header = _sign(body, ten_minutes_ago_ms)
    with pytest.raises(InvalidRetellSignature):
        verify_retell_signature(body, header, API_KEY, max_skew_seconds=300)


def test_future_timestamp_outside_skew_rejected():
    body = b'{"event":"call_ended"}'
    future_ms = int((time.time() + 600) * 1000)
    header = _sign(body, future_ms)
    with pytest.raises(InvalidRetellSignature):
        verify_retell_signature(body, header, API_KEY, max_skew_seconds=300)
