"""Forgot-password end to end: delivery, secrecy of the token, expiry, reuse, failure handling, SMTP transport."""
import logging
import re
import uuid
from types import SimpleNamespace

import pytest
import redis
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core import email as email_mod
from app.main import app
from tests.test_tenant_isolation import _signup

settings = get_settings()
PW = "correct-horse-battery-staple"
NEW_PW = "a-brand-new-passphrase"
REQUEST = "/api/v1/auth/password-reset/request"
CONFIRM = "/api/v1/auth/password-reset/confirm"
anon = TestClient(app)


def _account():
    tag = uuid.uuid4().hex[:10]
    email = f"reset-{tag}@reset-test.com"
    c, _org = _signup(email, f"Reset Org {tag}")
    return c, email


def _login(email, password):
    return TestClient(app).post("/api/v1/auth/login", json={"email": email, "password": password})


def _token(body: str) -> str:
    return re.search(r"token=([\w-]+)", body).group(1)


@pytest.fixture
def mail(monkeypatch):
    sent = []
    monkeypatch.setattr("app.api.v1.account.send_email", lambda to, subject, body: sent.append((to, subject, body)) or True)
    return sent


def _redis():
    return redis.from_url(settings.REDIS_URL, decode_responses=True)


# ---------------- the reported bug: "sent" but nothing arrives, and the link lands in the log ----------------

def test_without_smtp_nothing_is_sent_and_the_reset_link_is_not_logged(monkeypatch, caplog):
    monkeypatch.setattr(settings, "SMTP_HOST", "")
    caplog.set_level(logging.DEBUG)
    _c, email = _account()
    assert anon.post(REQUEST, json={"email": email}).status_code == 204
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "NOT sent" in text  # the operator is told delivery did not happen
    assert "reset-password" not in text and "token=" not in text  # ...but the credential is never logged
    assert email not in text  # full address masked
    # and no token was left usable by a log reader: the only copy was in the (unsent) body
    assert "r***@reset-test.com" in text


def test_a_provider_failure_still_answers_204_and_is_logged_without_secrets(monkeypatch, caplog):
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(settings, "SMTP_PORT", 587)
    monkeypatch.setattr(settings, "SMTP_USE_SSL", False)
    monkeypatch.setattr(email_mod, "in_test_mode", lambda: False)  # this test exercises the transport's failure path with a FAKE smtplib

    class Boom:
        def __init__(self, *a, **k):
            raise ConnectionRefusedError("refused for secret-address@leak.test")

    monkeypatch.setattr(email_mod.smtplib, "SMTP", Boom)
    monkeypatch.setattr(email_mod.smtplib, "SMTP_SSL", Boom)
    caplog.set_level(logging.DEBUG)
    _c, email = _account()
    r = anon.post(REQUEST, json={"email": email})
    assert r.status_code == 204 and r.text == ""
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "failed" in text and "ConnectionRefusedError" in text
    assert "token=" not in text and "leak.test" not in text and email not in text


def test_unknown_account_gets_the_same_answer_and_no_email(mail):
    _c, email = _account()
    known = anon.post(REQUEST, json={"email": email})
    unknown = anon.post(REQUEST, json={"email": f"ghost-{uuid.uuid4().hex[:8]}@reset-test.com"})
    assert (known.status_code, known.text, dict(known.headers).get("content-length")) == (
        unknown.status_code, unknown.text, dict(unknown.headers).get("content-length"))
    assert [m[0] for m in mail] == [email]


def test_the_link_is_built_from_configuration_not_from_request_headers(mail):
    _c, email = _account()
    anon.post(REQUEST, json={"email": email}, headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example"})
    assert mail[0][2].count(f"{settings.FRONTEND_URL}/reset-password?token=") == 1
    assert "evil.example" not in mail[0][2]


# ---------------- tokens ----------------

def test_the_token_is_stored_hashed_and_expires(mail):
    _c, email = _account()
    anon.post(REQUEST, json={"email": email})
    token = _token(mail[0][2])
    import hashlib

    r = _redis()
    assert not any(token in k for k in r.scan_iter("pwreset*"))  # the raw token is not a key anywhere
    key = "pwreset:" + hashlib.sha256(token.encode()).hexdigest()
    assert r.get(key) is not None  # only the hash is stored
    assert 0 < r.ttl(key) <= settings.PASSWORD_RESET_TTL_SECONDS
    r.pexpire(key, 1)  # let it lapse
    import time

    time.sleep(0.05)
    assert anon.post(CONFIRM, json={"token": token, "new_password": NEW_PW}).status_code == 400
    assert _login(email, PW).status_code == 200  # nothing changed


def test_a_weak_password_does_not_burn_the_token(mail):
    _c, email = _account()
    anon.post(REQUEST, json={"email": email})
    token = _token(mail[0][2])
    assert anon.post(CONFIRM, json={"token": token, "new_password": "short"}).status_code == 422
    assert anon.post(CONFIRM, json={"token": token, "new_password": NEW_PW}).status_code == 204


def test_success_changes_the_password_once_signs_out_old_sessions_and_confirms_by_email(mail):
    c, email = _account()
    anon.post(REQUEST, json={"email": email})
    token = _token(mail[0][2])
    assert c.get("/api/v1/auth/me").status_code == 200
    assert anon.post(CONFIRM, json={"token": token, "new_password": NEW_PW}).status_code == 204
    assert c.get("/api/v1/auth/me").status_code == 401
    assert _login(email, PW).status_code == 401 and _login(email, NEW_PW).status_code == 200
    # reuse
    assert anon.post(CONFIRM, json={"token": token, "new_password": "yet-another-passphrase"}).status_code == 400
    assert _login(email, NEW_PW).status_code == 200
    # the password-changed confirmation: to the account, no token, no password
    assert [m[0] for m in mail] == [email, email]
    to, subject, body = mail[1]
    assert subject == "Your Atla password was changed"
    assert token not in body and NEW_PW not in body and "token=" not in body
    assert f"{settings.FRONTEND_URL}/forgot-password" in body


def test_no_confirmation_email_when_the_token_is_invalid(mail):
    _c, email = _account()
    assert anon.post(CONFIRM, json={"token": "x" * 40, "new_password": NEW_PW}).status_code == 400
    assert mail == []


def test_confirm_attempts_are_rate_limited():
    codes = [anon.post(CONFIRM, json={"token": "x" * 40, "new_password": NEW_PW}).status_code for _ in range(22)]
    assert codes[:20] == [400] * 20 and codes[20:] == [429, 429]


def test_a_failed_confirmation_email_does_not_undo_the_reset(monkeypatch):
    sent = []

    def flaky(to, subject, body):
        sent.append(subject)
        return subject.startswith("Reset")  # reset mail arrives, the "changed" mail fails

    monkeypatch.setattr("app.api.v1.account.send_email", flaky)
    bodies = []
    real = flaky
    monkeypatch.setattr("app.api.v1.account.send_email", lambda to, s, b: bodies.append(b) or real(to, s, b))
    _c, email = _account()
    anon.post(REQUEST, json={"email": email})
    assert anon.post(CONFIRM, json={"token": _token(bodies[0]), "new_password": NEW_PW}).status_code == 204
    assert _login(email, NEW_PW).status_code == 200
    assert sent == ["Reset your Atla password", "Your Atla password was changed"]


# ---------------- SMTP transport ----------------

class _FakeSMTP:
    instances: list = []

    def __init__(self, host, port, timeout=None):
        self.calls = [("connect", host, port)]
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user))

    def send_message(self, msg):
        self.calls.append(("send", msg["To"], msg["Subject"]))


def _smtp(monkeypatch, **overrides):
    cfg = dict(SMTP_HOST="smtp.example.test", SMTP_PORT=587, SMTP_USERNAME="u", SMTP_PASSWORD="p",
               SMTP_FROM="Atla <no-reply@example.test>", SMTP_STARTTLS=True, SMTP_USE_SSL=False, is_production=False)
    cfg.update(overrides)
    _FakeSMTP.instances = []
    monkeypatch.setattr(email_mod, "get_settings", lambda: SimpleNamespace(**cfg))
    monkeypatch.setattr(email_mod, "in_test_mode", lambda: False)  # these tests exercise the transport itself, against a FAKE smtplib
    monkeypatch.setattr(email_mod.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(email_mod.smtplib, "SMTP_SSL", _FakeSMTP)


def test_port_587_uses_starttls_then_login_then_send(monkeypatch):
    _smtp(monkeypatch)
    assert email_mod.send_email("a@b.test", "Subj", "body") is True
    assert [c[0] for c in _FakeSMTP.instances[0].calls] == ["connect", "starttls", "login", "send"]


def test_port_465_uses_implicit_tls_never_starttls(monkeypatch):
    _smtp(monkeypatch, SMTP_PORT=465)
    created = []
    monkeypatch.setattr(email_mod.smtplib, "SMTP", lambda *a, **k: created.append("plain") or _FakeSMTP(*a, **k))
    assert email_mod.send_email("a@b.test", "Subj", "body") is True
    assert created == [] and [c[0] for c in _FakeSMTP.instances[0].calls] == ["connect", "login", "send"]


def test_local_catcher_without_tls_or_login(monkeypatch):
    _smtp(monkeypatch, SMTP_PORT=1025, SMTP_STARTTLS=False, SMTP_USERNAME="")
    assert email_mod.send_email("a@b.test", "Subj", "body") is True
    assert [c[0] for c in _FakeSMTP.instances[0].calls] == ["connect", "send"]


def test_a_rejected_send_returns_false(monkeypatch):
    _smtp(monkeypatch)

    def reject(self, msg):
        raise OSError("550 rejected")

    monkeypatch.setattr(_FakeSMTP, "send_message", reject)
    assert email_mod.send_email("a@b.test", "Subj", "body") is False


def test_mask_address():
    assert email_mod.mask_address("jane.doe@example.com") == "j***@example.com"
    assert email_mod.mask_address("nonsense") == "***"


def test_send_test_email_command_reports_honestly(monkeypatch, capsys):
    from app.workers import send_test_email

    monkeypatch.setattr(send_test_email, "in_test_mode", lambda: False)  # the command refuses in test mode (tests/test_email_safety.py)
    monkeypatch.setattr(send_test_email, "get_settings", lambda: SimpleNamespace(SMTP_HOST="", SMTP_PORT=587))
    assert send_test_email.main(["--to", "a@b.test"]) == 2
    monkeypatch.setattr(send_test_email, "get_settings", lambda: SimpleNamespace(
        SMTP_HOST="h", SMTP_PORT=587, SMTP_USE_SSL=False, SMTP_STARTTLS=True, SMTP_FROM="x"))
    monkeypatch.setattr(send_test_email, "send_email", lambda *a: False)
    assert send_test_email.main(["--to", "a@b.test"]) == 1
    monkeypatch.setattr(send_test_email, "send_email", lambda *a: True)
    assert send_test_email.main(["--to", "a@b.test"]) == 0
    assert "Accepted" in capsys.readouterr().out
