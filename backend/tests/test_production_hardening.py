"""Task 12 network/ops hardening: body limit, production fail-closed checks, error-tracking privacy."""
import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import app.main  # noqa: F401  (registers every model before the session schema is created)
from app.config import Settings
from app.core.body_limit import BodyLimitMiddleware
from app.core.error_tracking import redact, scrub_event
from tests.test_production_config import GOOD, _settings  # noqa: F401


# ---------------- body limit ----------------
def _app(default=1000, webhook=5000):
    app = FastAPI()
    app.add_middleware(BodyLimitMiddleware, default_limit=default, webhook_limit=webhook)

    @app.post("/echo")
    async def echo(request: Request):
        return {"n": len(await request.body())}

    @app.post("/webhooks/x")
    async def hook(request: Request):
        return {"n": len(await request.body())}

    return TestClient(app)


def test_a_body_within_the_limit_passes_untouched():
    c = _app()
    assert c.post("/echo", content=b"a" * 1000).json() == {"n": 1000}


def test_a_declared_oversize_body_is_refused_before_it_is_read():
    r = _app().post("/echo", content=b"a" * 1001)
    assert r.status_code == 413 and r.json() == {"detail": "Request body too large."}


def test_a_chunked_body_without_a_length_is_cut_off_when_it_exceeds_the_limit():
    def gen():
        for _ in range(30):
            yield b"a" * 100

    r = _app().post("/echo", content=gen())
    assert r.status_code == 413


def test_a_malformed_content_length_is_refused():
    r = _app().post("/echo", content=b"x", headers={"Content-Length": "abc"})
    assert r.status_code in (400, 413)


def test_webhook_paths_have_their_own_larger_ceiling():
    c = _app()
    assert c.post("/webhooks/x", content=b"a" * 4000).status_code == 200
    assert c.post("/webhooks/x", content=b"a" * 5001).status_code == 413
    assert c.post("/echo", content=b"a" * 4000).status_code == 413  # the larger ceiling is not available to ordinary routes


def test_the_real_app_refuses_a_two_megabyte_login_body_and_still_serves_normal_requests():
    from app.main import app

    c = TestClient(app)
    assert c.post("/api/v1/auth/login", content=b"{" + b" " * 2_000_000 + b"}", headers={"Content-Type": "application/json"}).status_code == 413
    assert c.post("/api/v1/auth/login", json={"email": "a@example.com", "password": "x" * 20}).status_code in (401, 429)


def test_the_real_app_applies_the_limit_to_webhooks_but_lets_a_large_signed_transcript_size_reach_signature_checking():
    from app.main import app

    c = TestClient(app)
    big = b"{" + b" " * 3_000_000 + b"}"  # larger than an ordinary route allows, below the webhook ceiling
    assert c.post("/webhooks/retell", content=big).status_code in (400, 401, 403)  # reached the signature check, not 413
    huge = b"{" + b" " * 11_000_000 + b"}"
    assert c.post("/webhooks/retell", content=huge).status_code == 413


# ---------------- production fail-closed ----------------
def test_a_wildcard_forwarded_allow_ips_is_refused_in_production(monkeypatch):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    assert any(p.startswith("FORWARDED_ALLOW_IPS") for p in _settings().production_config_problems())
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.5")
    assert not any(p.startswith("FORWARDED_ALLOW_IPS") for p in _settings().production_config_problems())
    monkeypatch.delenv("FORWARDED_ALLOW_IPS")
    assert not any(p.startswith("FORWARDED_ALLOW_IPS") for p in _settings().production_config_problems())


@pytest.mark.parametrize("over,name", [
    ({"MONITORING_TOKEN": "short"}, "MONITORING_TOKEN"),
    ({"ALERT_WEBHOOK_URL": "http://hooks.example.com/x"}, "ALERT_WEBHOOK_URL"),
    ({"SENTRY_DSN": "http://k@o.ingest.sentry.io/1"}, "SENTRY_DSN"),
])
def test_unsafe_monitoring_settings_are_refused_in_production(monkeypatch, over, name):
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    problems = _settings(**over).production_config_problems()
    assert any(p.startswith(name) for p in problems)
    assert not any(v in p for p in problems for v in over.values())  # values are never reported


def test_a_good_production_configuration_with_monitoring_is_accepted(monkeypatch):
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    ok = _settings(MONITORING_TOKEN="m" * 40, ALERT_WEBHOOK_URL="https://hooks.example.com/x", SENTRY_DSN="https://k@o.ingest.sentry.io/1")
    assert ok.production_config_problems() == []


@pytest.mark.parametrize("name,bad", [("MAX_REQUEST_BODY_BYTES", 10), ("MAX_REQUEST_BODY_BYTES", 10**10), ("MAX_WEBHOOK_BODY_BYTES", 0)])
def test_absurd_body_limits_are_refused(monkeypatch, name, bad):
    monkeypatch.setenv(name, str(bad))
    with pytest.raises(Exception):
        Settings()


def test_production_sets_secure_cookies_and_hsts_and_test_mode_does_not():
    import app.api.v1.auth as auth_mod
    from app.config import get_settings
    from app.core.security_headers import SecurityHeadersMiddleware  # noqa: F401

    prod = _settings()
    assert prod.is_production is True and get_settings().is_production is False
    import inspect

    assert "secure=settings.is_production" in inspect.getsource(auth_mod).replace(" ", "") or "secure=settings.is_production" in inspect.getsource(auth_mod)
    from app.core import security_headers

    assert 'if settings.is_production:' in inspect.getsource(security_headers) and "max-age=63072000" in inspect.getsource(security_headers)


# ---------------- error tracking privacy ----------------
def test_redact_removes_emails_numbers_and_tokens_from_text():
    out = redact("user jane@example.com called +1 (415) 555-0100 with Bearer abcdef0123456789abcdef0123456789abcd and card 4111 1111 1111 1111")
    for leak in ("jane@example.com", "555-0100", "abcdef0123456789", "4111 1111"):
        assert leak not in out


def test_scrub_event_drops_bodies_headers_cookies_query_user_and_locals():
    event = {
        "request": {"method": "POST", "url": "https://api.example.com/api/v1/x?token=SECRETTOKENVALUE1234567890abcdef", "data": {"password": "hunter2"},
                    "headers": {"Authorization": "Bearer zzz", "Cookie": "atla_session=abc"}, "cookies": {"atla_session": "abc"}, "query_string": "token=abc", "env": {"REMOTE_ADDR": "1.2.3.4"}},
        "user": {"email": "jane@example.com", "ip_address": "1.2.3.4"},
        "server_name": "host-1",
        "message": "failed for jane@example.com",
        "extra": {"transcript": "my SSN is 123", "note": "call +14155550100"},
        "exception": {"values": [{"type": "ValueError", "value": "bad number +14155550100 for jane@example.com",
                                  "stacktrace": {"frames": [{"function": "f", "vars": {"password": "hunter2", "api_key": "k", "address_line": "12 Oak Street Springfield"}}]}}]},
        "breadcrumbs": {"values": [{"message": "GET /x for jane@example.com", "data": {"cookie": "abc"}}]},
    }
    dumped = json.dumps(scrub_event(event))
    for leak in ("hunter2", "Bearer zzz", "atla_session=abc", "SECRETTOKENVALUE", "jane@example.com", "1.2.3.4", "123", "Oak Street", "4155550100", "host-1", "token=abc"):
        assert leak not in dumped, leak
    assert "ValueError" in dumped and "/api/v1/x" in dumped  # the useful part survives


def test_sentry_is_off_without_a_dsn_and_start_up_never_fails_on_a_bad_one(monkeypatch):
    from app.config import get_settings
    from app.core import error_tracking

    monkeypatch.setattr(get_settings(), "SENTRY_DSN", "")
    assert error_tracking.init_error_tracking("api") is False
    monkeypatch.setattr(get_settings(), "SENTRY_DSN", "not-a-dsn")
    assert error_tracking.init_error_tracking("api") is False  # invalid DSN: logged, application continues
    monkeypatch.setattr(error_tracking, "_initialised", False)
    error_tracking.report_exception(RuntimeError("x"))  # a no-op when tracking is off


def test_sdk_is_configured_without_pii_bodies_or_locals(monkeypatch):
    import sentry_sdk

    from app.config import get_settings
    from app.core import error_tracking

    seen = {}
    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: seen.update(kw))
    monkeypatch.setattr(sentry_sdk, "set_tag", lambda *a, **k: None)
    monkeypatch.setattr(get_settings(), "SENTRY_DSN", "https://k@o.ingest.sentry.io/1")
    assert error_tracking.init_error_tracking("worker") is True
    assert seen["send_default_pii"] is False and seen["include_local_variables"] is False
    assert seen["max_request_body_size"] == "never" and seen["before_send"] is error_tracking.scrub_event
    monkeypatch.setattr(error_tracking, "_initialised", False)
