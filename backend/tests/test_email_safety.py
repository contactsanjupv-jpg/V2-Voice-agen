"""
Automated tests must never send real email (docs/EMAIL_SAFETY.md).

Incident: the full suite sent receptionist-paused emails to synthetic addresses (credits-test.com, agent-test.com) through the real
Resend account configured in .env, using up the daily quota. Cause: tests that run the REAL worker pass (run_worker.run_once) drained
notification rows left behind by other tests, and core.email.send_email opened SMTP whenever SMTP_HOST was set, with no ENV check.

Everything here runs with VALID-LOOKING Resend settings configured and proves no connection is attempted. No real credential is used.
"""
import logging
import pathlib
import re
import smtplib
import socket
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.config import LOOPBACK_HOSTS, get_settings
from app.core import email as email_mod
from app.core.email import EmailOutcome, EmailResult, check_breaker, classify_smtp_error
from app.db.base import SessionLocal
from app.db.models.tenancy import OrgRole
from app.main import app
from app.services import owner_notifications as on
from app.workers import email_quota, run_worker
from fastapi.testclient import TestClient
from tests import email_safety
from tests.billing_helpers import new_org
from tests.fakes import FakePhone
from tests.test_owner_notifications import (
    FRONTEND, LEAD, _add_member, _analyzed, _apply, _credit_org, _credit_pass, _enforce, _live_org, _member_emails,
    _paused_setup, _rows, _send, _wipe,
)
from tests.test_inbound_call_resolution import _retell_key  # noqa: F401  (autouse: signs the Retell webhook the lead scenario posts)
from tests.test_password_reset_flow import CONFIRM, NEW_PW, REQUEST, _account, _token

RESEND = dict(SMTP_HOST="smtp.resend.com", SMTP_PORT=465, SMTP_USERNAME="resend", SMTP_PASSWORD="re_TEST_ONLY_not_a_real_credential",
              SMTP_USE_SSL=True, SMTP_STARTTLS=False, SMTP_FROM="Atla <no-reply@mail.example.test>")


@pytest.fixture(autouse=True)
def _outbox_kinds_are_email_eligible_for_these_tests(monkeypatch):
    """These tests exercise the email OUTBOX and the SMTP safety layers with the operational kinds as stand-ins. By policy none of them
    is email-eligible (on.EMAIL_KINDS is empty); that is asserted in tests/test_notification_policy.py."""
    from app.services import owner_notifications as on

    monkeypatch.setattr(on, "EMAIL_KINDS", frozenset({on.KIND_LEAD, on.KIND_SERVICE_PAUSED, on.KIND_LOW_CREDITS, on.KIND_CREDITS_EMPTY, on.KIND_PAYMENT_FAILED}))


@pytest.fixture
def resend_configured(monkeypatch):
    """The dangerous situation: valid-looking Resend settings present in the (already scrubbed) settings object."""
    settings = get_settings()
    for name, value in RESEND.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, "FRONTEND_URL", FRONTEND)
    monkeypatch.setattr(settings, "OWNER_NOTIFICATIONS_ENABLED", True)
    return settings


@pytest.fixture
def clean_outbox_table():
    _wipe()
    yield
    _wipe()


@pytest.fixture
def real_smtplib_recorders(monkeypatch):
    """Replaces smtplib's classes with recorders, so a connection that gets past test mode is COUNTED (and still goes nowhere)."""
    built: list[tuple[str, str, int]] = []

    class Recorder:
        def __init__(self, host="", port=0, *a, **k):
            built.append((type(self).__name__, host, port))
            raise ConnectionRefusedError("recorder: nothing is connected")

    class SMTP(Recorder):
        pass

    class SMTP_SSL(Recorder):  # noqa: N801 - mirrors smtplib
        pass

    monkeypatch.setattr(smtplib, "SMTP", SMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", SMTP_SSL)
    return built


# ------------------------------------------------------------------ 1. send_email refuses in test mode, whatever is configured

def _only(monkeypatch, trigger):
    """Test mode switched on by exactly ONE of its three triggers, the other two off."""
    monkeypatch.setattr(email_mod, "_force_test_mode", trigger == "forced")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    if trigger == "pytest-env":
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/x.py::t (call)")
    monkeypatch.setattr(get_settings(), "ENV", "test" if trigger == "env-test" else "development")


@pytest.mark.parametrize("trigger", ["env-test", "forced", "pytest-env"])
def test_each_test_mode_trigger_alone_stops_real_delivery_even_with_resend_credentials(monkeypatch, resend_configured, real_smtplib_recorders, trigger):
    _only(monkeypatch, trigger)
    assert email_mod.in_test_mode() is True
    result = email_mod.send_email_result("owner@credits-test.com", "Your Atla receptionist has been paused", "secret body")
    assert result.outcome is EmailOutcome.BLOCKED and email_mod.send_email("owner@credits-test.com", "x", "y") is False
    assert real_smtplib_recorders == []  # not one connection was even attempted
    assert ("o***@credits-test.com", "Your Atla receptionist has been paused") in email_mod.blocked_in_test_mode


def test_negative_control_without_test_mode_the_same_configuration_does_reach_smtp(monkeypatch, resend_configured, real_smtplib_recorders):
    """Proves the tests above pass because of the guard, not because the setup cannot send."""
    monkeypatch.setattr(email_mod, "in_test_mode", lambda: False)
    assert email_mod.send_email("owner@credits-test.com", "s", "b") is False  # the recorder refuses the connection
    assert real_smtplib_recorders == [("SMTP_SSL", "smtp.resend.com", 465)]


def test_a_test_catcher_is_ignored_outside_test_mode_so_it_can_never_hide_a_production_send(monkeypatch, resend_configured, real_smtplib_recorders, outbox):
    monkeypatch.setattr(email_mod, "in_test_mode", lambda: False)
    email_mod.send_email("owner@example.test", "s", "b")
    assert outbox.sent == [] and len(real_smtplib_recorders) == 1


def test_the_catcher_records_recipient_subject_and_body_and_can_refuse(resend_configured, real_smtplib_recorders, outbox):
    assert email_mod.send_email("a@example.test", "Hello", "line one") is True
    assert outbox.sent == [("a@example.test", "Hello", "line one")] and outbox.subjects() == ["Hello"]
    outbox.refuse = {"b@example.test"}
    assert email_mod.send_email_result("b@example.test", "Hello", "x").outcome is EmailOutcome.TRANSIENT
    assert real_smtplib_recorders == []


# ------------------------------------------------------------------ 2. the suite-level guard (layer 2) and the scrub (layer 3)

def test_real_credentials_are_scrubbed_from_the_settings_the_tests_see():
    s = get_settings()
    assert (s.SMTP_HOST, s.SMTP_USERNAME, s.SMTP_PASSWORD) == ("", "", "")


def test_the_guard_refuses_every_way_of_opening_a_connection_to_a_real_mail_server_and_records_it():
    attempts = [
        lambda: smtplib.SMTP("smtp.resend.com", 587),
        lambda: smtplib.SMTP_SSL("smtp.resend.com", 465),
        lambda: socket.create_connection(("smtp.resend.com", 465)),
        lambda: socket.socket().connect(("smtp.resend.com", 587)),
        lambda: socket.socket().connect_ex(("203.0.113.9", 25)),
    ]
    for attempt in attempts:
        with pytest.raises(email_safety.RealSMTPBlocked):
            attempt()
    assert len(email_safety.take_violations()) == len(attempts)  # consumed here, so the autouse check does not fail THIS test


def test_a_swallowed_attempt_is_still_recorded_so_the_test_that_made_it_fails_at_teardown():
    try:
        smtplib.SMTP_SSL("smtp.resend.com", 465)
    except Exception:  # noqa: BLE001 - the pattern send_email itself uses
        pass
    assert email_safety.violations and "smtp.resend.com:465" in email_safety.violations[0]
    email_safety.take_violations()


def test_loopback_catchers_and_ordinary_ports_are_not_blocked():
    assert all(email_safety.is_loopback(h) for h in ("localhost", "127.0.0.1", "::1", "127.0.0.5"))
    assert not email_safety.is_loopback("smtp.resend.com") and not email_safety.is_loopback("203.0.113.9")
    with pytest.raises(OSError) as refused:  # a closed local mail port: refused by the OS, not by the guard
        socket.create_connection(("127.0.0.1", 2525), timeout=1)
    assert not isinstance(refused.value, email_safety.RealSMTPBlocked) and email_safety.violations == []


def test_test_mode_with_a_real_smtp_host_is_refused_at_startup_without_leaking_the_password():
    base = get_settings()
    bad = base.model_copy(update=dict(ENV="test", SMTP_HOST="smtp.resend.com", SMTP_PASSWORD="re_TEST_ONLY_not_a_real_credential"))
    for process in ("api", "worker"):
        with pytest.raises(RuntimeError, match="ENV=test is refused") as why:
            bad.assert_startup_safe(process)
        assert "re_TEST_ONLY" not in str(why.value)
    for host in ("", *sorted(LOOPBACK_HOSTS)):
        base.model_copy(update=dict(ENV="test", SMTP_HOST=host)).assert_startup_safe("worker")  # consistent: fine


# ------------------------------------------------------------------ 3. the worker and every notification path, with Resend configured

def test_the_real_worker_pass_cannot_send_leftover_notifications(resend_configured, real_smtplib_recorders, clean_outbox_table, monkeypatch):
    """The incident path: run_worker.run_once drains pending rows left by other tests. Here: a real paused-receptionist row."""
    org, provider = _paused_setup(monkeypatch)
    assert _enforce(org, provider)["suspended"] == 1  # entitlement.py enqueues service_paused
    summary = run_worker.run_once(phone_provider=FakePhone())
    assert summary["failed_jobs"] == [] and real_smtplib_recorders == []
    (n,) = _rows(org, "service_paused")
    assert (n.status, n.last_error) == ("skipped", "test mode: email is never sent")  # drained safely, not retried, not sent
    assert any(subject == "Your Atla receptionist has been paused" for _who, subject in email_mod.blocked_in_test_mode)


def _build_lead():
    org, agent, number = _live_org()
    _analyzed(org, agent, number, LEAD)
    return org, "New lead: Dana Plumber", ("Dana Plumber", "+15551230000", f"{FRONTEND}/dashboard/leads")


def _build_paused(monkeypatch):
    org, provider = _paused_setup(monkeypatch)
    _enforce(org, provider)
    return org, "Your Atla receptionist has been paused", ("minutes have run out", f"{FRONTEND}/dashboard/go-live")


def _build_low(monkeypatch):
    org = _credit_org(monkeypatch, used_minutes=85)
    _credit_pass(org)
    return org, "Your Atla call minutes are running low", ("about 15 minutes", f"{FRONTEND}/dashboard/settings/billing")


def _build_empty(monkeypatch):
    org = _credit_org(monkeypatch, used_minutes=100)
    _credit_pass(org)
    return org, "You have used all your Atla call minutes", ("used all your call minutes",)


def _build_payment_failed(monkeypatch):
    monkeypatch.setattr(get_settings(), "PADDLE_STARTER_PRICE_ID", "pri_starter")
    _c, org = new_org("payx")
    _apply(org, f"sub_{uuid.uuid4().hex[:8]}", "past_due", datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc))
    return org, "Action needed: your Atla payment failed", ("Sun Nov 8, 2026 12:00 PM UTC", f"{FRONTEND}/dashboard/settings/billing")


BUILDERS = {"lead": _build_lead, "service_paused": _build_paused, "low_credits": _build_low, "credits_empty": _build_empty, "payment_failed": _build_payment_failed}


@pytest.mark.parametrize("kind", list(BUILDERS))
def test_every_owner_notification_kind_reaches_the_catcher_with_the_right_content_and_never_smtp(kind, monkeypatch, resend_configured, real_smtplib_recorders, clean_outbox_table, outbox):
    built = BUILDERS[kind]
    org, subject, fragments = built() if kind == "lead" else built(monkeypatch)
    owners = set(_member_emails(org))
    assert _send()["sent"] >= 1
    mine = [m for m in outbox.sent if m[0] in owners]
    assert mine, f"no {kind} email for this organization"
    assert {m[1] for m in mine} == {subject}
    for fragment in fragments:
        assert fragment in mine[0][2]
    assert real_smtplib_recorders == [] and email_mod.blocked_in_test_mode == []
    assert [n.status for n in _rows(org, kind)] == ["sent"]


@pytest.mark.parametrize("kind", list(BUILDERS))
def test_every_owner_notification_kind_without_a_catcher_is_blocked_not_sent_and_not_retried(kind, monkeypatch, resend_configured, real_smtplib_recorders, clean_outbox_table):
    org, subject, _fragments = BUILDERS[kind]() if kind == "lead" else BUILDERS[kind](monkeypatch)
    _send()
    assert real_smtplib_recorders == []
    (n,) = _rows(org, kind)
    assert (n.status, n.attempts) == ("skipped", 1)  # one pass, one refusal, finished: no retry storm
    assert any(s == subject for _who, s in email_mod.blocked_in_test_mode)
    assert _send()["retry"] == 0 and _rows(org, kind)[0].attempts == 1


def test_password_reset_and_confirmation_emails_reach_the_catcher_not_smtp(resend_configured, real_smtplib_recorders, outbox):
    client = TestClient(app)
    _c, address = _account()
    assert client.post(REQUEST, json={"email": address}).status_code == 204
    (to, subject, body) = outbox.to(address)[0]
    assert subject == "Reset your Atla password" and f"{FRONTEND}/reset-password?token=" in body
    assert client.post(CONFIRM, json={"token": _token(body), "new_password": NEW_PW}).status_code == 204
    assert [m[1] for m in outbox.to(address)] == ["Reset your Atla password", "Your Atla password was changed"]
    assert real_smtplib_recorders == []


def test_password_reset_without_a_catcher_still_answers_204_and_sends_nothing(resend_configured, real_smtplib_recorders):
    _c, address = _account()
    assert TestClient(app).post(REQUEST, json={"email": address}).status_code == 204
    assert real_smtplib_recorders == [] and ("r***@reset-test.com", "Reset your Atla password") in email_mod.blocked_in_test_mode


def test_the_send_test_email_command_refuses_in_test_mode(resend_configured, real_smtplib_recorders, capsys):
    from app.workers import send_test_email

    assert send_test_email.main(["--to", "a@example.test"]) == 2
    assert "nothing was sent" in capsys.readouterr().out and real_smtplib_recorders == []


# ------------------------------------------------------------------ 4. classifying failures

@pytest.mark.parametrize(
    "exc, expected",
    [
        (smtplib.SMTPAuthenticationError(535, b"bad credentials"), EmailOutcome.PERMANENT),
        (smtplib.SMTPRecipientsRefused({"x@y.test": (550, b"no such user")}), EmailOutcome.PERMANENT),
        (smtplib.SMTPRecipientsRefused({"x@y.test": (452, b"Daily quota exceeded")}), EmailOutcome.QUOTA),
        (smtplib.SMTPSenderRefused(553, b"sender not allowed", "a@b.test"), EmailOutcome.PERMANENT),
        (smtplib.SMTPDataError(554, b"Your daily sending quota has been reached"), EmailOutcome.QUOTA),
        (smtplib.SMTPDataError(550, b"message rejected"), EmailOutcome.PERMANENT),
        (smtplib.SMTPResponseException(451, b"temporary local problem"), EmailOutcome.TRANSIENT),
        (smtplib.SMTPResponseException(429, b"Too many requests, slow down"), EmailOutcome.TRANSIENT),  # rate limiting is not a daily quota
        (smtplib.SMTPServerDisconnected("gone"), EmailOutcome.TRANSIENT),
        (TimeoutError(), EmailOutcome.TRANSIENT),
        (ConnectionResetError(), EmailOutcome.TRANSIENT),
        (ValueError("anything unrecognised"), EmailOutcome.TRANSIENT),
    ],
)
def test_smtp_failures_are_classified(exc, expected):
    assert classify_smtp_error(exc).outcome is expected


# These strings are REALISTIC WORDING, written for this test. They are not captured from Resend or any provider: Resend's actual SMTP
# reply text has NOT been verified (no live call is made), so the classifier relies on plain words, not on any provider's format.
@pytest.mark.parametrize(
    "code, text, outcome, kind",
    [
        (550, "Daily quota exceeded", EmailOutcome.QUOTA, "daily"),
        (554, "You have reached your daily quota", EmailOutcome.QUOTA, "daily"),
        (550, "daily_quota_exceeded", EmailOutcome.QUOTA, "daily"),
        (452, "Daily sending limit reached for today", EmailOutcome.QUOTA, "daily"),
        (550, "5.4.5 Daily user sending quota exceeded", EmailOutcome.QUOTA, "daily"),
        (550, "Monthly quota exceeded", EmailOutcome.QUOTA, "monthly"),
        (554, "You have reached your monthly sending limit", EmailOutcome.QUOTA, "monthly"),
        (550, "monthly_quota_exceeded", EmailOutcome.QUOTA, "monthly"),
        (554, "Monthly and daily quota exhausted", EmailOutcome.QUOTA, "monthly"),
        (550, "Sending quota exceeded", EmailOutcome.QUOTA, "unknown"),
        (554, "Quota exceeded for this account", EmailOutcome.QUOTA, "unknown"),
        (429, "Too many requests", EmailOutcome.TRANSIENT, None),
        (451, "Rate limit exceeded, retry later", EmailOutcome.TRANSIENT, None),
        (421, "Please slow down", EmailOutcome.TRANSIENT, None),
        (450, "Throttled: try again", EmailOutcome.TRANSIENT, None),
        (550, "Mailbox unavailable", EmailOutcome.PERMANENT, None),
        (552, "The limit of the mailbox size was exceeded", EmailOutcome.PERMANENT, None),
        (550, "Mailbox full", EmailOutcome.PERMANENT, None),
    ],
)
def test_realistic_provider_wording_is_classified_into_daily_monthly_unknown_or_rate_limit(code, text, outcome, kind):
    for exc in (smtplib.SMTPDataError(code, text.encode()), smtplib.SMTPRecipientsRefused({"x@y.test": (code, text.encode())})):
        result = classify_smtp_error(exc)
        assert (result.outcome, result.quota_kind) == (outcome, kind), text


def test_only_a_timezone_aware_reset_time_is_taken_from_server_text():
    assert email_mod.server_stated_reset("resets at 2026-12-01T00:00:00Z") == datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert email_mod.server_stated_reset("resets 2026-12-01 05:30:00+05:30") == datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert email_mod.server_stated_reset("resets at 2026-12-01T00:00:00") is None  # no timezone: not trusted
    assert email_mod.server_stated_reset("try again tomorrow") is None


def test_a_classified_result_never_carries_server_text_or_addresses():
    leak = smtplib.SMTPRecipientsRefused({"secret-person@leak.test": (550, b"secret-person@leak.test does not exist")})
    assert "leak.test" not in classify_smtp_error(leak).detail
    assert "leak.test" not in classify_smtp_error(smtplib.SMTPDataError(550, b"nope secret-person@leak.test")).detail


class _QuotaSMTP:
    constructed = 0

    def __init__(self, *a, **k):
        type(self).constructed += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, *a):
        pass

    def send_message(self, msg):
        raise smtplib.SMTPDataError(554, b"Daily sending quota exceeded")


@pytest.fixture
def quota_transport(monkeypatch):
    _QuotaSMTP.constructed = 0
    monkeypatch.setattr(email_mod, "in_test_mode", lambda: False)
    monkeypatch.setattr(email_mod.smtplib, "SMTP_SSL", _QuotaSMTP)
    monkeypatch.setattr(get_settings(), "SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(get_settings(), "SMTP_PORT", 465)
    return _QuotaSMTP


def test_a_quota_response_stops_all_sending_until_the_pause_ends_or_an_operator_clears_it(quota_transport):
    first = email_mod.send_email_result("a@example.test", "s", "b")
    assert first.outcome is EmailOutcome.QUOTA and first.quota_kind == "daily" and first.retry_after is not None and quota_transport.constructed == 1
    assert check_breaker().pause.until == first.retry_after
    for _ in range(5):  # every later send, from any process sharing Redis, is refused WITHOUT contacting the provider
        again = email_mod.send_email_result("a@example.test", "s", "b")
        assert again.outcome is EmailOutcome.QUOTA and again.retry_after == first.retry_after
    assert quota_transport.constructed == 1
    assert email_mod.clear_quota_pause() is True and check_breaker().pause is None
    email_mod.send_email_result("a@example.test", "s", "b")
    assert quota_transport.constructed == 2  # an operator's clear lets exactly one attempt through (and it pauses again)


def test_a_daily_quota_without_a_stated_reset_waits_for_the_next_utc_midnight_and_at_least_half_an_hour():
    noon = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
    pause = email_mod.pause_for_quota("daily", now=noon)
    assert (pause.until, pause.source) == (datetime(2026, 11, 20, 0, 5, tzinfo=timezone.utc), "convention")
    late = datetime(2026, 11, 19, 23, 50, tzinfo=timezone.utc)
    assert email_mod.pause_for_quota("daily", now=late).until == late + timedelta(minutes=30)


@pytest.mark.parametrize("kind", ["monthly", "unknown"])
def test_a_monthly_or_unknown_quota_never_resumes_by_itself_only_an_operator_resumes_it(kind):
    now = datetime.now(timezone.utc)
    pause = email_mod.pause_for_quota(kind, now=now)
    assert pause.until is None and pause.operator_required and pause.source == "operator-required"
    for later in (now + timedelta(days=1, minutes=6), now + timedelta(days=40), now + timedelta(days=400)):
        assert check_breaker(later).pause == pause  # still paused: tomorrow is NOT assumed to be a reset
    assert email_mod.clear_quota_pause() is True and check_breaker().pause is None


def test_a_reset_time_the_server_states_is_used_whatever_the_kind_and_a_dubious_one_is_not():
    now = datetime.now(timezone.utc)
    stated = now + timedelta(days=9)
    for kind in ("monthly", "daily", "unknown"):
        pause = email_mod.pause_for_quota(kind, stated, now=now)
        assert (pause.until, pause.source) == (stated, "server")
    email_mod.clear_quota_pause()
    assert email_mod.pause_for_quota("monthly", now - timedelta(hours=1), now=now).until is None  # already in the past: not believed
    email_mod.clear_quota_pause()
    assert email_mod.pause_for_quota("monthly", now + timedelta(days=400), now=now).until is None  # absurdly far: not believed
    email_mod.clear_quota_pause()
    assert email_mod.pause_for_quota("daily", now - timedelta(hours=1), now=now).source == "convention"


def test_a_monthly_quota_from_the_transport_pauses_sending_until_an_operator_clears_it(quota_transport, monkeypatch):
    def refuse(self, msg):
        raise smtplib.SMTPDataError(554, b"Monthly quota exceeded")

    monkeypatch.setattr(quota_transport, "send_message", refuse)
    first = email_mod.send_email_result("a@example.test", "s", "b")
    assert (first.outcome, first.quota_kind, first.retry_after) == (EmailOutcome.QUOTA, "monthly", None)
    assert email_mod.send_email_result("a@example.test", "s", "b").outcome is EmailOutcome.QUOTA and quota_transport.constructed == 1
    assert check_breaker(datetime.now(timezone.utc) + timedelta(days=3)).pause.operator_required


def test_a_reset_time_in_the_servers_message_is_honoured(quota_transport, monkeypatch):
    reset = (datetime.now(timezone.utc) + timedelta(days=6)).replace(microsecond=0)

    def refuse(self, msg):
        raise smtplib.SMTPDataError(554, f"Monthly quota exceeded. Resets at {reset.isoformat()}".encode())

    monkeypatch.setattr(quota_transport, "send_message", refuse)
    result = email_mod.send_email_result("a@example.test", "s", "b")
    assert (result.quota_kind, result.retry_after) == ("monthly", reset)


def test_ordinary_rate_limiting_never_pauses_sending(quota_transport, monkeypatch):
    def refuse(self, msg):
        raise smtplib.SMTPResponseException(429, b"Too many requests, rate limit exceeded")

    monkeypatch.setattr(quota_transport, "send_message", refuse)
    result = email_mod.send_email_result("a@example.test", "s", "b")
    assert result.outcome is EmailOutcome.TRANSIENT and "rate limited" in result.detail
    assert check_breaker().pause is None
    email_mod.send_email_result("a@example.test", "s", "b")
    assert quota_transport.constructed == 2  # the next send IS attempted


def test_the_operator_command_reports_and_clears_the_pause(capsys):
    assert email_quota.main(["status"]) == 0 and "not paused" in capsys.readouterr().out
    email_mod.pause_for_quota("monthly")
    assert email_quota.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "PAUSED" in out and "monthly" in out and "No automatic resume" in out
    assert email_quota.main(["clear"]) == 0 and "cleared" in capsys.readouterr().out
    assert check_breaker().pause is None and email_quota.main(["nonsense"]) == 2


# ------------------------------------------------------------------ 5. notification retries: permanent, quota, storms

class Scripted:
    """Stands in for send_email_result inside the notification service; answers from a script and counts calls."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, to, subject, body):
        self.calls.append(to)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return answer


def _one_paused_row(prefix="rq"):
    _c, org = new_org(prefix)
    db = SessionLocal()
    try:
        assert on.enqueue(db, uuid.UUID(org), on.KIND_SERVICE_PAUSED, f"k-{uuid.uuid4().hex[:6]}", {"reason": on.REASON_CREDITS})
        db.commit()
    finally:
        db.close()
    return org


def test_a_permanent_failure_is_final_and_is_never_retried(monkeypatch, resend_configured, clean_outbox_table):
    org = _one_paused_row()
    script = Scripted(EmailResult(EmailOutcome.PERMANENT, "SMTPAuthenticationError 535"))
    monkeypatch.setattr(on, "send_email_result", script)
    assert _send()["failed"] == 1
    (n,) = _rows(org)
    assert (n.status, n.attempts, n.next_attempt_at) == ("failed", 1, None) and "permanent" in n.last_error and "535" in n.last_error
    later = datetime.now(timezone.utc) + timedelta(days=2)
    assert _send(now=later) == {"sent": 0, "skipped": 0, "failed": 0, "retry": 0} and len(script.calls) == 1


def test_a_quota_failure_pauses_the_notification_without_using_up_its_attempts(monkeypatch, resend_configured, clean_outbox_table):
    org = _one_paused_row()
    _add_member(org, OrgRole.admin)  # two recipients: the second must not even be tried once the quota is gone
    reset_at = datetime.now(timezone.utc) + timedelta(hours=9)
    script = Scripted(EmailResult(EmailOutcome.QUOTA, "SMTPDataError 554", retry_after=reset_at))
    monkeypatch.setattr(on, "send_email_result", script)
    _send()
    assert len(script.calls) == 1
    (n,) = _rows(org)
    assert (n.status, n.attempts) == ("pending", 0) and n.next_attempt_at == reset_at and "quota" in n.last_error
    assert _send(now=reset_at - timedelta(minutes=1))["retry"] == 0 and len(script.calls) == 1  # nothing before the reset
    monkeypatch.setattr(on, "send_email_result", Scripted(EmailResult(EmailOutcome.SENT)))
    assert _send(now=reset_at + timedelta(minutes=1))["sent"] == 1
    assert _rows(org)[0].status == "sent"


def test_while_the_quota_breaker_is_set_the_worker_touches_nothing(monkeypatch, resend_configured, clean_outbox_table):
    org = _one_paused_row()
    script = Scripted(EmailResult(EmailOutcome.SENT))
    monkeypatch.setattr(on, "send_email_result", script)
    until = email_mod.pause_for_quota("daily").until
    result = _send()
    assert result["paused_until"] == until.isoformat() and result["quota_kind"] == "daily" and (result["sent"], result["retry"]) == (0, 0) and script.calls == []
    (n,) = _rows(org)
    assert (n.status, n.attempts) == ("pending", 0)  # waiting, intact
    email_mod.clear_quota_pause()
    assert _send()["sent"] >= 1 and _rows(org)[0].status == "sent"


def test_a_failing_provider_is_not_hammered_a_pass_stops_after_five_failures_in_a_row(monkeypatch, resend_configured, clean_outbox_table):
    _c, org = new_org("storm")
    db = SessionLocal()
    try:
        for i in range(8):
            assert on.enqueue(db, uuid.UUID(org), on.KIND_SERVICE_PAUSED, f"storm-{i}", {"reason": on.REASON_CREDITS})
        db.commit()
    finally:
        db.close()
    script = Scripted(EmailResult(EmailOutcome.TRANSIENT, "SMTPServerDisconnected"))
    monkeypatch.setattr(on, "send_email_result", script)
    result = _send()
    assert len(script.calls) == 5 and result["retry"] == 5 and result["stopped_early"] is True
    assert sorted(n.attempts for n in _rows(org)) == [0, 0, 0, 1, 1, 1, 1, 1]  # the other three were never touched
    assert all(n.status == "pending" for n in _rows(org))


def test_a_transient_failure_still_backs_off_and_is_delivered_later_exactly_once(monkeypatch, resend_configured, clean_outbox_table):
    org = _one_paused_row()
    script = Scripted(EmailResult(EmailOutcome.TRANSIENT, "TimeoutError"), EmailResult(EmailOutcome.SENT))
    monkeypatch.setattr(on, "send_email_result", script)
    start = datetime.now(timezone.utc)
    assert _send(now=start)["retry"] == 1
    assert _send(now=start + timedelta(seconds=5))["retry"] == 0  # not due yet
    assert _send(now=start + timedelta(minutes=5))["sent"] == 1
    assert _send(now=start + timedelta(hours=2)) == {"sent": 0, "skipped": 0, "failed": 0, "retry": 0}  # delivered once, then done
    assert len(script.calls) == 2 and _rows(org)[0].status == "sent"


def test_an_unconfigured_mail_server_is_still_a_retryable_failure_as_before(monkeypatch, resend_configured, clean_outbox_table):
    org = _one_paused_row()
    monkeypatch.setattr(on, "send_email_result", Scripted(EmailResult(EmailOutcome.NOT_CONFIGURED)))
    assert _send()["retry"] == 1
    (n,) = _rows(org)
    assert n.status == "pending" and n.last_error == "email was not accepted by the mail server"


def test_a_repeated_cause_never_queues_a_second_row_so_retries_cannot_multiply_emails(monkeypatch, clean_outbox_table):
    _c, org = new_org("idem")
    db = SessionLocal()
    try:
        results = [on.enqueue(db, uuid.UUID(org), on.KIND_SERVICE_PAUSED, "same-cause", {"reason": on.REASON_CREDITS}) for _ in range(4)]
        db.commit()
    finally:
        db.close()
    assert results == [True, False, False, False] and len(_rows(org)) == 1


def test_production_behaviour_is_unchanged_when_not_in_test_mode(monkeypatch, resend_configured):
    """With test mode off and a (fake) server that accepts, the real transport sends exactly as before."""
    sent = []

    class Accepting(_QuotaSMTP):
        def send_message(self, msg):
            sent.append((msg["To"], msg["Subject"], msg.get_content().strip()))

    monkeypatch.setattr(email_mod, "in_test_mode", lambda: False)
    monkeypatch.setattr(email_mod.smtplib, "SMTP_SSL", Accepting)
    assert email_mod.send_email("owner@example.test", "Reset your Atla password", "link") is True
    assert sent == [("owner@example.test", "Reset your Atla password", "link")]


def test_blocked_sends_are_logged_without_the_body(monkeypatch, caplog, resend_configured):
    caplog.set_level(logging.DEBUG)
    email_mod.send_email("someone@example.test", "Reset your Atla password", "https://x/reset-password?token=SECRET123")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "NOT sent" in text and "SECRET123" not in text and "someone@example.test" not in text


# ------------------------------------------------------------------ 6. several recipients: partial delivery, quota, recovery

class PerAddress:
    """send_email_result stand-in that answers per recipient and records every call."""

    def __init__(self, default=None, **by_address):
        self.default = default or EmailResult(EmailOutcome.SENT)
        self.by_address, self.calls = by_address, []

    def __call__(self, to, subject, body):
        self.calls.append(to)
        answer = self.by_address.get(to, self.default)
        return answer(to) if callable(answer) else answer


def _team(prefix="team"):
    """An organization with a paused-receptionist notification queued and THREE recipients (owner + two admins); sorted addresses."""
    org = _one_paused_row(prefix)
    _add_member(org, OrgRole.admin)
    _add_member(org, OrgRole.admin)
    return org, sorted(_member_emails(org))


def _delivered(org):
    return len(_rows(org)[0].payload.get("_delivery", {}).get("sent", []))


def test_partial_delivery_then_quota_then_recovery_sends_each_recipient_exactly_once(monkeypatch, resend_configured, clean_outbox_table):
    org, (a, b, c) = _team()
    quota = EmailResult(EmailOutcome.QUOTA, "SMTPDataError 554 daily quota", retry_after=datetime.now(timezone.utc) + timedelta(hours=6), quota_kind="daily")
    first = PerAddress(**{b: quota})  # a is accepted, b hits the quota, c is never tried
    monkeypatch.setattr(on, "send_email_result", first)
    _send()
    assert first.calls == [a, b]
    (n,) = _rows(org)
    assert (n.status, n.attempts, _delivered(org)) == ("pending", 0, 1) and "daily quota" in n.last_error and n.sent_at is None
    later = datetime.now(timezone.utc) + timedelta(hours=7)
    second = PerAddress()
    monkeypatch.setattr(on, "send_email_result", second)
    assert _send(now=later)["sent"] == 1
    assert second.calls == [b, c]  # a is NOT emailed again
    assert (_rows(org)[0].status, _delivered(org)) == ("sent", 3)
    assert _send(now=later + timedelta(days=1)) == {"sent": 0, "skipped": 0, "failed": 0, "retry": 0}  # finished: nothing more, ever
    assert sorted(first.calls[:1] + second.calls) == [a, b, c]  # each address received exactly one email in total


def test_a_monthly_quota_mid_delivery_waits_for_an_operator_and_then_finishes_with_the_rest_only(monkeypatch, resend_configured, clean_outbox_table):
    org, (a, b, c) = _team("mq")
    monthly = EmailResult(EmailOutcome.QUOTA, "monthly quota", retry_after=None, quota_kind="monthly")
    monkeypatch.setattr(on, "send_email_result", PerAddress(**{b: monthly}))
    start = datetime.now(timezone.utc)
    _send(now=start)
    (n,) = _rows(org)
    assert n.status == "pending" and _delivered(org) == 1 and "operator" in n.last_error
    pause = email_mod.pause_for_quota("monthly")  # what the real transport would have recorded
    assert pause.operator_required
    rest = PerAddress()
    monkeypatch.setattr(on, "send_email_result", rest)
    month_later = start + timedelta(days=35)
    result = _send(now=month_later)
    assert result["paused_until"] == "operator" and rest.calls == []  # still nothing, a month later
    email_mod.clear_quota_pause()
    assert _send(now=month_later)["sent"] == 1 and rest.calls == [b, c]


def test_a_transient_failure_for_one_recipient_retries_only_that_recipient(monkeypatch, resend_configured, clean_outbox_table):
    org, (a, b, c) = _team("tr")
    flaky = PerAddress(**{b: EmailResult(EmailOutcome.TRANSIENT, "TimeoutError")})
    monkeypatch.setattr(on, "send_email_result", flaky)
    start = datetime.now(timezone.utc)
    assert _send(now=start)["retry"] == 1
    assert flaky.calls == [a, b, c] and _rows(org)[0].status == "pending" and _delivered(org) == 2  # the others were not held back
    assert "1 of 3 recipients still to receive it" in _rows(org)[0].last_error
    fixed = PerAddress()
    monkeypatch.setattr(on, "send_email_result", fixed)
    assert _send(now=start + timedelta(minutes=5))["sent"] == 1 and fixed.calls == [b]


def test_one_permanently_refused_recipient_never_blocks_or_repeats_the_others(monkeypatch, resend_configured, clean_outbox_table):
    org, (a, b, c) = _team("pm")
    calls = PerAddress(**{b: EmailResult(EmailOutcome.PERMANENT, "SMTPRecipientsRefused")})
    monkeypatch.setattr(on, "send_email_result", calls)
    assert _send()["sent"] == 1
    (n,) = _rows(org)
    assert n.status == "sent" and _delivered(org) == 2 and "permanently refused" in n.last_error and calls.calls == [a, b, c]
    assert _send(now=datetime.now(timezone.utc) + timedelta(days=1))["sent"] == 0 and len(calls.calls) == 3


def test_a_crash_after_one_recipient_was_sent_does_not_resend_to_them(monkeypatch, resend_configured, clean_outbox_table):
    org, (a, b, c) = _team("cr")

    def explode(address):
        raise RuntimeError("worker crashed")

    boom = PerAddress(**{b: explode})
    monkeypatch.setattr(on, "send_email_result", boom)
    start = datetime.now(timezone.utc)
    _send(now=start)
    assert boom.calls == [a, b] and _delivered(org) == 1 and _rows(org)[0].status == "pending"  # a's delivery was committed first
    ok = PerAddress()
    monkeypatch.setattr(on, "send_email_result", ok)
    _send(now=start + timedelta(hours=2))
    assert ok.calls == [b, c] and _rows(org)[0].status == "sent"


def test_a_row_being_sent_is_leased_so_a_second_worker_cannot_send_it_at_the_same_time(monkeypatch, resend_configured, clean_outbox_table):
    org, _addresses = _team("ls")
    start = datetime.now(timezone.utc)
    seen = []

    def during(to, subject, body):
        if not seen:  # a second worker passes over the row while the first is mid-send (the first has committed a recipient)
            seen.append(on._process(SessionLocal(), _rows(org)[0].id, start))
        return EmailResult(EmailOutcome.SENT)

    monkeypatch.setattr(on, "send_email_result", during)
    _send(now=start)
    assert seen == [None] and _rows(org)[0].status == "sent"


def test_the_recipient_record_stores_no_email_address(monkeypatch, resend_configured, clean_outbox_table):
    org, addresses = _team("pi")
    monkeypatch.setattr(on, "send_email_result", PerAddress())
    _send()
    text = str(_rows(org)[0].payload)
    assert "_delivery" in text and not any(address in text or address.split("@")[0] in text for address in addresses)


# ------------------------------------------------------------------ 7. the shared quota state: durable, and fail closed when unreadable

@pytest.fixture
def dead_store(monkeypatch):
    """The database holding the shared quota state cannot be read or written."""

    def fail(*a, **k):
        raise email_mod.BreakerStoreError("OperationalError")

    monkeypatch.setattr(email_mod, "_store_read", fail)
    monkeypatch.setattr(email_mod, "_store_write", fail)
    monkeypatch.setattr(email_mod, "_gate_store", fail)
    return fail


@pytest.fixture
def no_redis(monkeypatch):
    """Redis is unreachable. The quota breaker must not depend on it at all."""
    import redis

    def refuse(*a, **k):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(redis, "from_url", refuse)


def _another_process(monkeypatch):
    """A different process has no in-memory state of its own; only what is in the shared database."""
    email_mod._replace_local(None)


def test_a_monthly_pause_survives_redis_going_down_and_coming_back_and_a_fresh_process(quota_transport, monkeypatch, no_redis):
    """The sequence that matters: A detects the monthly quota while Redis is down; B tries; Redis recovers; B tries again."""
    def refuse(self, msg):
        raise smtplib.SMTPDataError(554, b"Monthly quota exceeded")

    monkeypatch.setattr(quota_transport, "send_message", refuse)
    a = email_mod.send_email_result("a@example.test", "s", "b")  # process A: pause recorded with Redis unreachable
    assert (a.outcome, a.quota_kind, a.retry_after) == (EmailOutcome.QUOTA, "monthly", None) and quota_transport.constructed == 1
    _another_process(monkeypatch)  # process B
    held = email_mod.send_email_result("a@example.test", "s", "b")
    assert held.outcome is EmailOutcome.QUOTA and held.quota_kind == "monthly" and quota_transport.constructed == 1
    monkeypatch.undo()  # Redis recovers (and the transport fakes are restored)
    monkeypatch.setattr(email_mod, "in_test_mode", lambda: False)
    monkeypatch.setattr(email_mod.smtplib, "SMTP_SSL", quota_transport)
    monkeypatch.setattr(get_settings(), "SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(get_settings(), "SMTP_PORT", 465)
    _another_process(monkeypatch)
    again = email_mod.send_email_result("a@example.test", "s", "b")
    assert again.outcome is EmailOutcome.QUOTA and again.quota_kind == "monthly" and quota_transport.constructed == 1  # still paused
    assert check_breaker(datetime.now(timezone.utc) + timedelta(days=30)).pause.operator_required  # and still a month later
    assert email_mod.clear_quota_pause() is True  # only an operator (or a server-stated reset) ends it
    assert check_breaker().pause is None


def test_the_database_going_down_while_the_pause_is_recorded_does_not_lose_it(quota_transport, monkeypatch):
    """A detects the monthly quota but the database refuses the write. B cannot read the state, so it is held. When the database
    returns, the pause is published and B stays paused. Nothing resumes by itself."""
    def refuse(self, msg):
        raise smtplib.SMTPDataError(554, b"Monthly quota exceeded")

    monkeypatch.setattr(quota_transport, "send_message", refuse)
    real_read, real_write = email_mod._store_read, email_mod._store_write
    monkeypatch.setattr(email_mod, "_PERSIST_DELAY", 0)

    def down(*a, **k):
        raise email_mod.BreakerStoreError("OperationalError")

    with monkeypatch.context() as outage:
        outage.setattr(email_mod, "_store_write", down)  # reads still work, so the first send is attempted
        first = email_mod.send_email_result("a@example.test", "s", "b")  # process A
        assert first.outcome is EmailOutcome.QUOTA and quota_transport.constructed == 1 and email_mod._get_local() is not None
        outage.setattr(email_mod, "_store_read", down)  # now the database is unreachable altogether
        outage.setattr(email_mod, "_gate_store", down)
        a_again = email_mod.send_email_result("a@example.test", "s", "b")
        assert a_again.outcome is EmailOutcome.QUOTA and quota_transport.constructed == 1  # A's own pause holds
        mine = email_mod._get_local()
        email_mod._replace_local(None)  # process B: no memory of its own
        b = email_mod.send_email_result("a@example.test", "s", "b")
        assert b.outcome is EmailOutcome.HELD and quota_transport.constructed == 1  # fail closed
        email_mod._replace_local(mine)
    assert (email_mod._store_read, email_mod._store_write) == (real_read, real_write)  # the database is back
    assert email_mod.check_breaker().pause == mine and email_mod._get_local() is None  # A published the pause it was holding
    _another_process(monkeypatch)
    after = email_mod.send_email_result("a@example.test", "s", "b")  # process B again
    assert after.outcome is EmailOutcome.QUOTA and after.quota_kind == "monthly" and quota_transport.constructed == 1
    assert check_breaker(datetime.now(timezone.utc) + timedelta(days=40)).pause.operator_required


def test_a_pause_that_has_not_been_cleared_outlasts_a_newer_unrelated_audit_entry(quota_transport):
    email_mod.pause_for_quota("monthly")
    db = SessionLocal()
    try:
        from app.db.models.platform import AuditLog

        db.add(AuditLog(action="refund.submitted", target_type="refund_request", audit_metadata={}))  # unrelated, newer
        db.commit()
    finally:
        db.close()
    assert check_breaker().pause is not None and check_breaker().pause.kind == "monthly"


def test_the_clear_command_writes_an_audit_entry_and_a_newer_pause_wins_over_an_older_clear(capsys):
    from app.db.models.platform import AuditLog

    email_mod.pause_for_quota("monthly")
    assert email_quota.main(["clear"]) == 0 and check_breaker().pause is None
    email_mod.pause_for_quota("unknown")
    assert check_breaker().pause.kind == "unknown"  # pause -> clear -> pause: the newest event decides
    db = SessionLocal()
    try:
        actions = [r.action for r in db.query(AuditLog).filter(AuditLog.action.in_((email_mod.PAUSE_ACTION, email_mod.CLEAR_ACTION))).order_by(AuditLog.created_at).all()]
    finally:
        db.close()
    assert actions[-3:] == [email_mod.PAUSE_ACTION, email_mod.CLEAR_ACTION, email_mod.PAUSE_ACTION]


def test_the_quota_breaker_does_not_use_redis_at_all(quota_transport, monkeypatch, no_redis):
    assert check_breaker().available is True
    assert email_mod.send_email_result("a@example.test", "s", "b").outcome is EmailOutcome.QUOTA and quota_transport.constructed == 1


def test_when_the_shared_state_cannot_be_read_nothing_is_sent_and_the_provider_is_not_contacted(quota_transport, dead_store):
    assert check_breaker().available is False
    result = email_mod.send_email_result("a@example.test", "s", "b")
    assert result.outcome is EmailOutcome.HELD and quota_transport.constructed == 0
    assert email_mod.send_email("a@example.test", "s", "b") is False and quota_transport.constructed == 0


def test_a_corrupt_pause_entry_is_unreadable_not_ignored(quota_transport):
    from app.db.models.platform import AuditLog

    db = SessionLocal()
    try:
        row = AuditLog(action=email_mod.PAUSE_ACTION, target_type="email_provider", audit_metadata={"kind": "monthly", "until": "garbage", "seq": 10**9})
        db.add(row)
        db.commit()
        row_id = row.id
    finally:
        db.close()
    try:
        assert check_breaker().available is False
        assert email_mod.send_email_result("a@example.test", "s", "b").outcome is EmailOutcome.HELD and quota_transport.constructed == 0
    finally:
        db = SessionLocal()
        db.query(AuditLog).filter(AuditLog.id == row_id).delete()
        db.commit()
        db.close()


def test_the_worker_pass_touches_nothing_while_the_shared_state_is_unreadable(monkeypatch, resend_configured, clean_outbox_table):
    org = _one_paused_row("rd")
    script = Scripted(EmailResult(EmailOutcome.SENT))
    monkeypatch.setattr(on, "send_email_result", script)
    with monkeypatch.context() as outage:
        def down(*a, **k):
            raise email_mod.BreakerStoreError("OperationalError")

        outage.setattr(email_mod, "_store_read", down)
        outage.setattr(email_mod, "_gate_store", down)
        result = _send()
    assert result["breaker_unavailable"] is True and (result["sent"], result["retry"]) == (0, 0) and script.calls == []
    assert (_rows(org)[0].status, _rows(org)[0].attempts) == ("pending", 0)


def test_a_held_notification_keeps_its_attempts_and_is_retried_in_minutes(monkeypatch, resend_configured, clean_outbox_table):
    org = _one_paused_row("hd")
    monkeypatch.setattr(on, "send_email_result", Scripted(EmailResult(EmailOutcome.HELD, "quota breaker unavailable")))
    start = datetime.now(timezone.utc)
    _send(now=start)
    (n,) = _rows(org)
    assert (n.status, n.attempts) == ("pending", 0) and "cannot be read" in n.last_error
    assert start + timedelta(minutes=4) < n.next_attempt_at <= start + timedelta(minutes=6)
    monkeypatch.setattr(on, "send_email_result", Scripted(EmailResult(EmailOutcome.SENT)))
    assert _send(now=start + timedelta(minutes=7))["sent"] == 1


def test_the_operator_commands_work_against_the_durable_state_and_report_an_unreadable_one(monkeypatch, capsys, dead_store):
    assert email_quota.main(["status"]) == 0 and "UNKNOWN" in capsys.readouterr().out
    assert email_quota.main(["clear"]) == 1 and "Nothing was cleared" in capsys.readouterr().out


# ------------------------------------------------------------------ 7b. atomic claim of a notification (PostgreSQL row locking)

def test_only_one_of_two_simultaneous_claims_succeeds_and_the_second_waits_for_the_first(clean_outbox_table):
    """Real PostgreSQL, two independent connections. A claims and has NOT committed; B's claim must block on A's row lock, and once A
    commits B must re-check the committed row and find it already claimed (0 rows). This is the READ COMMITTED behaviour the claim
    relies on, exercised for real: SQLite would not show it."""
    import threading

    org = _one_paused_row("claim")
    nid = _rows(org)[0].id
    now = datetime.now(timezone.utc)
    a, b = SessionLocal(), SessionLocal()
    outcome = {}

    def second():
        try:
            outcome["claimed"] = on._claim(b, nid, now)
            b.rollback()
        finally:
            outcome["done"] = True

    try:
        assert on._claim(a, nid, now) is not None  # A holds the row lock, uncommitted
        thread = threading.Thread(target=second)
        thread.start()
        thread.join(timeout=0.7)
        assert thread.is_alive() and "done" not in outcome  # B is waiting on A's lock, not racing past it
        a.commit()
        thread.join(timeout=10)
        assert outcome == {"claimed": None, "done": True}
    finally:
        a.close()
        b.close()
    n = _rows(org)[0]
    assert (n.status, n.attempts) == ("pending", 1) and n.next_attempt_at > now  # claimed exactly once, leased


def test_two_workers_processing_the_same_notification_at_once_send_it_exactly_once(monkeypatch, resend_configured, clean_outbox_table):
    import threading
    import time

    _c, org = new_org("race")
    db = SessionLocal()
    try:
        for i in range(6):
            assert on.enqueue(db, uuid.UUID(org), on.KIND_SERVICE_PAUSED, f"race-{i}", {"reason": on.REASON_CREDITS})
        db.commit()
    finally:
        db.close()
    ids = [n.id for n in _rows(org)]
    sends = []
    lock = threading.Lock()

    def slow_send(to, subject, body):
        time.sleep(0.15)  # long enough that a racing worker would still be inside its own send
        with lock:
            sends.append(to)
        return EmailResult(EmailOutcome.SENT)

    monkeypatch.setattr(on, "send_email_result", slow_send)
    now = datetime.now(timezone.utc)
    for nid in ids:
        results, gate = [], threading.Barrier(2)

        def worker():
            db = SessionLocal()
            try:
                gate.wait()
                results.append(on._process(db, nid, now))
            finally:
                db.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        [t.start() for t in threads]
        [t.join(timeout=30) for t in threads]
        assert sorted(results, key=str) == [None, "sent"], results  # one sent it, the other never touched it
    assert [(n.status, n.attempts) for n in _rows(org)] == [("sent", 1)] * 6
    assert len(sends) == 6  # one email per notification: no row was sent by both workers


def test_a_claim_expires_so_a_dead_worker_does_not_strand_a_notification(clean_outbox_table):
    org = _one_paused_row("lease")
    nid = _rows(org)[0].id
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        assert on._claim(db, nid, now) is not None
        db.commit()
        assert on._claim(db, nid, now + timedelta(minutes=9)) is None  # lease still running
        db.rollback()
        assert on._claim(db, nid, now + timedelta(minutes=11)) is not None  # the first worker is presumed dead
        db.commit()
    finally:
        db.close()
    assert _rows(org)[0].attempts == 2


# ------------------------------------------------------------------ 8. what the test guard covers (see docs/EMAIL_SAFETY.md)

def test_the_guard_also_covers_lmtp_and_a_subclass_of_smtp():
    class MySMTP(smtplib.SMTP):
        pass

    for attempt in (lambda: smtplib.LMTP("mail.example.test", 24), lambda: MySMTP("mail.example.test", 25), lambda: smtplib.SMTP("mail.example.test")):
        with pytest.raises(email_safety.RealSMTPBlocked):
            attempt()
    assert len(email_safety.take_violations()) == 3


def test_smtplib_is_imported_by_exactly_one_module_and_no_other_mail_provider_client_exists():
    """The guard blocks mail PORTS and smtplib. An HTTPS email API (port 443) is not blocked by it, so the real protection against
    that is this inventory: the only code in app/ that can send email is app/core/email.py, and it uses smtplib only."""
    app_dir = pathlib.Path(__file__).resolve().parent.parent / "app"
    transports = re.compile(r"smtplib|aiosmtplib|sendgrid|mailgun|postmark|resend|boto3|sendmail|fastapi_mail|yagmail|api\.mailchannels|mandrill", re.I)
    offenders = sorted(str(f.relative_to(app_dir)) for f in app_dir.rglob("*.py") if transports.search(f.read_text()))
    assert offenders == ["core/email.py"], offenders
    callers = sorted(str(f.relative_to(app_dir)) for f in app_dir.rglob("*.py") if re.search(r"from app\.core\.email import[^\n]*\bsend_email", f.read_text()))
    assert callers == ["api/v1/account.py", "services/owner_notifications.py", "workers/send_test_email.py"], callers


# ------------------------------------------------------------------ 9. writes failing while reads still work (the breaker store is read-only)

def _read_only_sessions(monkeypatch):
    """Every session the breaker opens behaves like a connection to a read-only replica or a full disk: SELECT works, any write is
    refused by PostgreSQL itself ('cannot execute INSERT in a read-only transaction')."""
    from sqlalchemy import event, text

    from app.db import base

    real = base.SessionLocal

    def factory():
        session = real()
        event.listen(session, "after_begin", lambda s, t, connection: connection.execute(text("SET TRANSACTION READ ONLY")))
        return session

    monkeypatch.setattr(base, "SessionLocal", factory)


PROCESS_B = r"""
import json, smtplib, sys
from app.config import get_settings
from app.core import email as e

class Provider:
    contacted = 0
    def __init__(self, *a, **k): Provider.contacted += 1
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def login(self, *a): pass
    def send_message(self, m): pass

e.in_test_mode = lambda: False          # a real (non-test) sender, against a FAKE provider that counts contacts
smtplib.SMTP_SSL = Provider
s = get_settings(); s.SMTP_HOST, s.SMTP_PORT = "smtp.example.test", 465
r = e.send_email_result("someone@example.test", "subject", "body")
print(json.dumps({"outcome": r.outcome.value, "kind": r.quota_kind, "contacted": Provider.contacted}))
"""


def _process_b(read_only: bool) -> dict:
    """A genuinely separate Python process. read_only=True gives it a PostgreSQL connection whose transactions are read-only (libpq's
    PGOPTIONS), so for that process reads work and writes fail."""
    import json
    import os
    import subprocess
    import sys

    env = {**os.environ, "ENV": "test"}
    if read_only:
        env["PGOPTIONS"] = "-c default_transaction_read_only=on"
    done = subprocess.run([sys.executable, "-c", PROCESS_B], env=env, cwd=pathlib.Path(__file__).resolve().parent.parent, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_reads_work_but_the_pause_could_not_be_written_so_another_process_is_not_allowed_to_send(quota_transport, monkeypatch):
    """The exact scenario: A reads the database fine, detects the MONTHLY quota, and writing the pause to PostgreSQL fails. B has no
    local pause and CAN read the database, but the pause was never persisted. B must not contact the provider."""
    def monthly(self, msg):
        raise smtplib.SMTPDataError(554, b"Monthly quota exceeded")

    monkeypatch.setattr(quota_transport, "send_message", monthly)
    monkeypatch.setattr(email_mod, "_PERSIST_DELAY", 0)
    real_write = email_mod._store_write

    def refuse_pause_write(*a, **k):
        raise email_mod.BreakerStoreError("ReadOnlySqlTransaction")

    # --- process A: read OK, gate write OK, provider says monthly quota, the pause write fails
    assert check_breaker(for_sending=True).available is True  # A can read (and write) when it starts
    monkeypatch.setattr(email_mod, "_store_write", refuse_pause_write)
    a = email_mod.send_email_result("a@example.test", "s", "b")
    assert a.outcome is EmailOutcome.QUOTA and a.quota_kind == "monthly" and quota_transport.constructed == 1
    assert email_mod._get_local() is not None and email_mod._store_read() is None  # never persisted
    # --- process B: no memory of A, reads work, but the database refuses writes
    monkeypatch.setattr(email_mod, "_store_write", real_write)
    _another_process(monkeypatch)
    _read_only_sessions(monkeypatch)
    assert email_mod._store_read() is None  # B really can read, and really sees no pause
    b = email_mod.send_email_result("a@example.test", "s", "b")
    assert b.outcome is EmailOutcome.HELD and quota_transport.constructed == 1  # the provider was NOT contacted
    assert email_mod.send_email("a@example.test", "s", "b") is False and quota_transport.constructed == 1


def test_a_separate_process_with_a_read_only_database_connection_does_not_contact_the_provider():
    """Same condition, but B is a real operating-system process whose PostgreSQL connection is read-only."""
    result = _process_b(read_only=True)
    assert result == {"outcome": "held", "kind": None, "contacted": 0}


def test_the_same_separate_process_with_a_writable_database_does_send_so_the_hold_is_caused_by_the_read_only_store():
    result = _process_b(read_only=False)
    assert result == {"outcome": "sent", "kind": None, "contacted": 1}


def test_a_separate_process_sees_a_pause_that_was_persisted_and_does_not_contact_the_provider():
    email_mod.pause_for_quota("monthly")
    assert _process_b(read_only=False) == {"outcome": "quota", "kind": "monthly", "contacted": 0}


def test_once_the_database_accepts_writes_again_the_held_pause_is_published_and_binds_every_process(quota_transport, monkeypatch):
    def monthly(self, msg):
        raise smtplib.SMTPDataError(554, b"Monthly quota exceeded")

    monkeypatch.setattr(quota_transport, "send_message", monthly)
    monkeypatch.setattr(email_mod, "_PERSIST_DELAY", 0)
    real_write = email_mod._store_write

    def refuse(*a, **k):
        raise email_mod.BreakerStoreError("ReadOnlySqlTransaction")

    monkeypatch.setattr(email_mod, "_store_write", refuse)  # the write fails after A has passed the writability gate
    a = email_mod.send_email_result("a@example.test", "s", "b")
    assert a.outcome is EmailOutcome.QUOTA and email_mod._get_local() is not None
    monkeypatch.setattr(email_mod, "_store_write", real_write)  # the database accepts writes again
    assert email_mod._store_read() is None  # still not persisted
    assert check_breaker(for_sending=True).pause.kind == "monthly" and email_mod._get_local() is None  # A published it
    assert _process_b(read_only=False) == {"outcome": "quota", "kind": "monthly", "contacted": 0}


# ------------------------------------------------------------------ 10. ordering of pause and clear events

def _events():
    from app.db.models.platform import AuditLog

    db = SessionLocal()
    try:
        rows = db.query(AuditLog).filter(AuditLog.action.in_((email_mod.PAUSE_ACTION, email_mod.CLEAR_ACTION))).all()
        return sorted(((r.audit_metadata["seq"], r.action, r.created_at) for r in rows), key=lambda e: e[0])
    finally:
        db.close()


def test_events_are_ordered_by_sequence_not_by_timestamp_so_equal_or_skewed_clocks_cannot_reorder_them():
    from sqlalchemy import text

    base_seq = email_mod._store_write(email_mod.CLEAR_ACTION, {"by": "test"})
    pause_seq = email_mod._store_write(email_mod.PAUSE_ACTION, {"kind": "monthly", "until": None, "source": "operator-required"})
    clear_seq = email_mod._store_write(email_mod.CLEAR_ACTION, {"by": "test"})
    assert base_seq < pause_seq < clear_seq and email_mod._store_read() is None
    pause2 = email_mod._store_write(email_mod.PAUSE_ACTION, {"kind": "monthly", "until": None, "source": "operator-required"})
    db = SessionLocal()
    try:  # collide every timestamp, then make the NEWEST pause look the OLDEST by the clock
        db.execute(text("UPDATE audit_logs SET created_at = now() WHERE action IN ('email.quota_pause','email.quota_clear')"))
        db.execute(text("UPDATE audit_logs SET created_at = now() - interval '1 day' WHERE action = 'email.quota_pause' AND (audit_metadata->>'seq')::bigint = :s"), {"s": pause2})
        db.commit()
    finally:
        db.close()
    assert email_mod._store_read() is not None and email_mod._store_read().seq == pause2  # the newest event still wins
    assert check_breaker().pause.operator_required


def test_concurrent_pauses_and_clears_get_unique_consecutive_sequence_numbers_and_the_highest_one_is_the_state():
    import threading

    start = len(_events())
    gate = threading.Barrier(8)
    seen, failures = [], []

    def worker(i):
        gate.wait()
        for n in range(8):
            try:
                if (i + n) % 2:
                    seen.append(email_mod._store_write(email_mod.PAUSE_ACTION, {"kind": "monthly", "until": None, "source": "operator-required"}))
                else:
                    seen.append(email_mod._store_write(email_mod.CLEAR_ACTION, {"by": "test"}))
            except Exception as exc:  # noqa: BLE001
                failures.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert failures == [] and len(seen) == 64
    assert len(set(seen)) == 64 and max(seen) - min(seen) == 63  # no duplicates, no gaps: a total order
    events = _events()
    assert len(events) == start + 64
    newest = events[-1]
    assert (email_mod._store_read() is not None) == (newest[1] == email_mod.PAUSE_ACTION)  # the state is the newest event, always


def _newer_pause(kind="monthly"):
    """A pause recorded after the one an operator looked at: the pause was cleared (by someone) and the quota failed again."""
    email_mod.clear_quota_pause()
    return email_mod.pause_for_quota(kind)


def test_a_clear_cannot_override_a_pause_committed_after_the_one_the_operator_looked_at():
    first = email_mod.pause_for_quota("monthly")
    newer = _newer_pause()  # cleared by someone else, then the quota failed again after the operator looked
    assert newer.seq > first.seq
    with pytest.raises(email_mod.StalePauseError):
        email_mod.clear_quota_pause(expected_seq=first.seq)
    assert check_breaker().pause is not None and check_breaker().pause.seq == newer.seq  # untouched
    assert email_mod.clear_quota_pause(expected_seq=newer.seq) is True and check_breaker().pause is None


def test_a_clear_racing_a_newer_pause_waits_for_it_and_is_then_refused(capsys):
    """Real PostgreSQL, two sessions: a newer pause is written but not yet committed when the operator's clear starts. The clear must
    wait for that writer (advisory lock), then see the newer pause and refuse, not wipe it."""
    import threading

    seen = email_mod.pause_for_quota("monthly")
    db = SessionLocal()
    outcome = {}

    def clear():
        try:
            outcome["result"] = email_mod.clear_quota_pause(expected_seq=seen.seq)
        except email_mod.StalePauseError:
            outcome["result"] = "stale"
        outcome["done"] = True

    try:
        newer = email_mod._append_event(db, email_mod.PAUSE_ACTION, {"kind": "monthly", "until": None, "source": "operator-required"})  # uncommitted
        thread = threading.Thread(target=clear)
        thread.start()
        thread.join(timeout=0.7)
        assert thread.is_alive() and "done" not in outcome  # the clear is blocked behind the pause writer
        db.commit()
        thread.join(timeout=10)
    finally:
        db.close()
    assert outcome == {"result": "stale", "done": True}
    assert check_breaker().pause.seq == newer


def test_the_operator_command_refuses_a_stale_clear_and_shows_the_entry_number(capsys):
    first = email_mod.pause_for_quota("monthly")
    assert email_quota.main(["status"]) == 0 and f"entry #{first.seq}" in capsys.readouterr().out
    newer = _newer_pause()
    assert email_quota.main(["clear", "--seq", str(first.seq)]) == 1 and "newer quota pause" in capsys.readouterr().out
    assert check_breaker().pause.seq == newer.seq
    assert email_quota.main(["clear", "--seq", str(newer.seq)]) == 0 and check_breaker().pause is None


def test_the_heartbeat_is_a_single_row_updated_in_place_not_one_row_per_email(quota_transport):
    from app.db.models.platform import AuditLog

    for _ in range(5):
        assert check_breaker(for_sending=True).available is True
    db = SessionLocal()
    try:
        assert db.query(AuditLog).filter(AuditLog.action == email_mod.PROBE_ACTION).count() == 1
    finally:
        db.close()


# ------------------------------------------------------------------ 11. a local emergency pause survives every failed clear

def _hold_a_local_pause(monkeypatch):
    """This process detected a monthly quota while the database refused to record it, so it holds the pause alone."""
    def refuse(*a, **k):
        raise email_mod.BreakerStoreError("ReadOnlySqlTransaction")

    real = email_mod._store_write
    monkeypatch.setattr(email_mod, "_PERSIST_DELAY", 0)
    monkeypatch.setattr(email_mod, "_store_write", refuse)
    pause = email_mod.pause_for_quota("monthly")
    monkeypatch.setattr(email_mod, "_store_write", real)
    assert email_mod._get_local() is not None and email_mod._get_local() == pause
    return pause


def _dead(*a, **k):
    raise email_mod.BreakerStoreError("OperationalError")


def _event_count():
    return len(_events())


def test_a_clear_that_cannot_read_the_database_keeps_the_local_pause(monkeypatch):
    held = _hold_a_local_pause(monkeypatch)
    monkeypatch.setattr(email_mod, "_store_read", _dead)
    with pytest.raises(email_mod.BreakerStoreError):
        email_mod.clear_quota_pause()
    assert email_mod._get_local() is held  # not discarded
    monkeypatch.setattr(email_mod, "_gate_store", _dead)
    assert check_breaker(for_sending=True).pause == held  # and it still blocks this process while the database is unreachable
    assert email_quota.main(["clear"]) == 1 and email_mod._get_local() is held


def test_a_clear_whose_database_write_fails_keeps_the_local_pause_and_the_recorded_pause(monkeypatch):
    stored = email_mod.pause_for_quota("monthly")  # recorded in the database
    held = QuotaPauseLocal = email_mod.QuotaPause("monthly", None, "operator-required")  # and a local one held by this process
    email_mod._replace_local(held)
    before = _event_count()
    monkeypatch.setattr(email_mod, "_store_write", _dead)
    with pytest.raises(email_mod.BreakerStoreError):
        email_mod.clear_quota_pause(expected_seq=stored.seq)
    assert email_mod._get_local() is QuotaPauseLocal
    monkeypatch.undo()
    assert _event_count() == before and check_breaker().pause.seq == stored.seq  # nothing was cleared in the database either


def test_a_stale_clear_keeps_the_local_pause(monkeypatch):
    first = email_mod.pause_for_quota("monthly")
    newer = _newer_pause("daily")  # weaker than the local monthly pause below, so the local pause is not covered
    held = email_mod.QuotaPause("monthly", None, "operator-required")
    email_mod._replace_local(held)
    with pytest.raises(email_mod.StalePauseError):
        email_mod.clear_quota_pause(expected_seq=first.seq)  # detected before the write
    assert email_mod._get_local() is held and email_mod._store_read().seq == newer.seq

    def stale_at_write(*a, **k):  # a newer pause lands between the read and the locked write
        raise email_mod.StalePauseError("newer pause")

    monkeypatch.setattr(email_mod, "_store_write", stale_at_write)
    with pytest.raises(email_mod.StalePauseError):
        email_mod.clear_quota_pause(expected_seq=newer.seq)
    assert email_mod._get_local() is held


def test_a_local_only_pause_is_kept_by_a_stale_expectation_and_dropped_only_by_an_explicit_clear(monkeypatch):
    held = _hold_a_local_pause(monkeypatch)
    assert email_mod._store_read() is None  # nothing in the database
    with pytest.raises(email_mod.StalePauseError):
        email_mod.clear_quota_pause(expected_seq=7)  # the operator looked at some other entry
    assert email_mod._get_local() is held
    assert email_mod.clear_quota_pause() is True and email_mod._get_local() is None  # an explicit clear is what removes it


def test_only_a_successful_unqualified_clear_removes_both_the_local_pause_and_the_recorded_one(monkeypatch):
    stored = email_mod.pause_for_quota("monthly")
    held = email_mod.QuotaPause("monthly", None, "operator-required")
    email_mod._replace_local(held)
    assert email_mod.clear_quota_pause(expected_seq=stored.seq) is True  # a qualified clear names only the recorded pause
    assert email_mod._get_local() is held and email_mod._store_read() is None
    assert email_mod.clear_quota_pause() is True  # an unqualified clear is the explicit "resume everything"
    assert email_mod._get_local() is None and check_breaker().pause is None


# ------------------------------------------------------------------ 12. the operator command accepts only its exact grammar

MALFORMED = [
    ["clear", "--seq"],
    ["clear", "--seq", "abc"],
    ["clear", "--seq", ""],
    ["clear", "--seq", "-5"],
    ["clear", "--seq", "+5"],
    ["clear", "--seq", "0"],
    ["clear", "--seq", "007x"],
    ["clear", "--seq", "1.5"],
    ["clear", "--seq", "1e3"],
    ["clear", "--seq", " 3"],
    ["clear", "--seq", "²"],  # a superscript two: str.isdigit() accepts it, a bigint does not
    ["clear", "--seq", "٣"],  # an Arabic-Indic digit
    ["clear", "--seq", "9" * 30],
    ["clear", "--seq", "1", "2"],
    ["clear", "--seq", "1", "--force"],
    ["clear", "7"],
    ["clear", "--seq=1"],
    ["clear", "--sequence", "1"],
    ["clear", "extra"],
    ["clear", "--seq", "1", "clear"],
    ["status", "extra"],
    ["status", "--seq", "1"],
    ["Clear"],
    ["--seq", "1"],
    ["nonsense"],
    [""],
]


@pytest.mark.parametrize("argv", MALFORMED, ids=[repr(a) for a in MALFORMED])
def test_a_malformed_operator_command_exits_2_and_changes_nothing(argv, capsys):
    pause = email_mod.pause_for_quota("monthly")
    before = _event_count()
    assert email_quota.main(argv) == 2
    out = capsys.readouterr().out
    assert "error:" in out and "cleared" not in out.lower().replace("never", "")
    assert _event_count() == before  # no event written
    assert check_breaker().pause is not None and check_breaker().pause.seq == pause.seq  # still paused, same entry


def test_a_malformed_seq_never_becomes_an_unqualified_clear_even_when_a_newer_pause_exists(capsys):
    email_mod.pause_for_quota("monthly")
    newer = email_mod.pause_for_quota("monthly")
    assert email_quota.main(["clear", "--seq", "abc"]) == 2
    assert check_breaker().pause.seq == newer.seq


def test_the_valid_forms_still_work(capsys):
    assert email_quota.main([]) == 0 and email_quota.main(["status"]) == 0
    pause = email_mod.pause_for_quota("monthly")
    assert email_quota.main(["clear", "--seq", str(pause.seq)]) == 0 and check_breaker().pause is None
    email_mod.pause_for_quota("monthly")
    assert email_quota.main(["clear"]) == 0 and check_breaker().pause is None


# ------------------------------------------------------------------ 13. a recorded pause and a newer local-only pause at the same time

def _recorded_pause_and_a_newer_local_one(quota_transport, monkeypatch):
    """The race, reproduced through the real send path. Process A passes the send gate. Process B then records a pause (entry N) in
    PostgreSQL. A's already-in-flight send gets a quota reply, but A's own pause write fails: A holds a newer pause only in memory."""
    real_write = email_mod._store_write
    recorded = []

    def refuse(*a, **k):
        raise email_mod.BreakerStoreError("ReadOnlySqlTransaction")

    def in_flight(self, msg):
        recorded.append(real_write(email_mod.PAUSE_ACTION, {"kind": "monthly", "until": None, "source": "operator-required"}))  # process B
        monkeypatch.setattr(email_mod, "_store_write", refuse)  # from now on A's write fails
        raise smtplib.SMTPDataError(554, b"Monthly quota exceeded")

    monkeypatch.setattr(quota_transport, "send_message", in_flight)
    monkeypatch.setattr(email_mod, "_PERSIST_DELAY", 0)
    result = email_mod.send_email_result("a@example.test", "s", "b")  # process A
    monkeypatch.setattr(email_mod, "_store_write", real_write)  # the database accepts writes again
    assert result.outcome is EmailOutcome.QUOTA and quota_transport.constructed == 1
    local = email_mod._get_local()
    assert local is not None and local.seq == 0  # A's newer pause exists only in memory
    stored = email_mod._store_read()
    assert stored is not None and stored.seq == recorded[0]  # B's pause is recorded as entry N
    return recorded[0], local


def test_a_sequence_qualified_clear_does_not_discard_a_newer_unrecorded_local_pause(quota_transport, monkeypatch):
    n, local = _recorded_pause_and_a_newer_local_one(quota_transport, monkeypatch)
    before = _event_count()
    assert email_mod.clear_quota_pause(expected_seq=n) is True  # the pause the operator inspected is cleared...
    assert _event_count() == before + 1 and email_mod._store_read() is None
    assert email_mod._get_local() is local  # ...but A's own, newer, unrecorded pause was NOT silently erased
    assert check_breaker().pause == local  # sending is still blocked by it
    published = check_breaker(for_sending=True).pause  # the next check publishes it, so every process now sees it
    assert published is not None and published.kind == "monthly" and email_mod._get_local() is None
    stored = email_mod._store_read()
    assert stored is not None and stored.seq > n and stored.operator_required  # a NEW recorded pause, after the cleared one
    assert quota_transport.constructed == 1 and email_mod.send_email_result("a@example.test", "s", "b").outcome is EmailOutcome.QUOTA


def test_a_qualified_clear_of_the_wrong_entry_changes_nothing_when_both_pauses_exist(quota_transport, monkeypatch):
    n, local = _recorded_pause_and_a_newer_local_one(quota_transport, monkeypatch)
    before = _event_count()
    with pytest.raises(email_mod.StalePauseError):
        email_mod.clear_quota_pause(expected_seq=n + 5)
    with pytest.raises(email_mod.StalePauseError):
        email_mod.clear_quota_pause(expected_seq=0)  # the local pause's seq is not a valid way to name the recorded one
    assert _event_count() == before and email_mod._get_local() is local and email_mod._store_read().seq == n


def test_an_unqualified_clear_with_both_pauses_resumes_everything_this_process_could_see(quota_transport, monkeypatch):
    """Intended behaviour: `clear` with no --seq is the operator's explicit "resume everything now": the recorded pause AND the local
    one this process was holding are both cleared."""
    n, local = _recorded_pause_and_a_newer_local_one(quota_transport, monkeypatch)
    assert email_mod.clear_quota_pause() is True
    assert email_mod._get_local() is None and email_mod._store_read() is None
    assert check_breaker(for_sending=True).pause is None  # nothing left to publish


def test_an_unqualified_clear_keeps_a_local_pause_that_appears_while_it_is_running(quota_transport, monkeypatch):
    """A send still in flight gets its quota reply (and a newer local pause) between the clear's read and its write."""
    stored = email_mod.pause_for_quota("monthly")
    older = email_mod.QuotaPause("monthly", None, "operator-required")
    newer = email_mod.QuotaPause("unknown", None, "operator-required")
    email_mod._replace_local(older)
    real_write = email_mod._store_write

    def write_while_a_send_fails(*a, **k):
        email_mod._replace_local(newer)  # the in-flight send's quota reply, held locally
        return real_write(*a, **k)

    monkeypatch.setattr(email_mod, "_store_write", write_while_a_send_fails)
    assert email_mod.clear_quota_pause() is True
    assert email_mod._get_local() is newer  # the pause the operator never saw is kept
    monkeypatch.setattr(email_mod, "_store_write", real_write)
    assert email_mod._store_read() is None and stored.seq > 0


def test_a_qualified_clear_from_the_operator_command_never_reaches_another_process_memory(quota_transport, monkeypatch, capsys):
    n, local = _recorded_pause_and_a_newer_local_one(quota_transport, monkeypatch)
    assert email_quota.main(["clear", "--seq", str(n)]) == 0
    assert email_mod._get_local() is local and email_mod._store_read() is None
    assert email_quota.main(["clear"]) == 0 and email_mod._get_local() is None


def test_a_clear_of_a_local_only_pause_keeps_a_newer_local_pause_that_appears_during_the_clear(monkeypatch):
    older = email_mod.QuotaPause("monthly", None, "operator-required")
    newer = email_mod.QuotaPause("unknown", None, "operator-required")
    email_mod._replace_local(older)
    real_read = email_mod._store_read

    def read_then_a_send_fails(*a, **k):
        result = real_read(*a, **k)
        email_mod._replace_local(newer)  # an in-flight send's quota reply lands right after the read
        return result

    monkeypatch.setattr(email_mod, "_store_read", read_then_a_send_fails)
    assert email_mod.clear_quota_pause() is True
    assert email_mod._get_local() is newer


# ------------------------------------------------------------------ 14. the local pause is only ever touched under one lock

class HookedLock:
    """A real lock that lets a test run code at an exact boundary: just BEFORE the Nth acquisition (nothing held) or just AFTER the Nth
    release. That is where another thread's quota reply could land. Code run from a hook may take the lock itself (it is not counted)."""

    def __init__(self):
        self.real = threading.Lock()
        self.count = 0
        self.before, self.after = {}, {}
        self._busy = False
        self._current = 0

    def _run(self, hook):
        if hook is not None:
            self._busy = True
            try:
                hook()
            finally:
                self._busy = False

    def __enter__(self):
        if not self._busy:
            self.count += 1
            self._current = self.count
            self._run(self.before.get(self._current))
        self.real.acquire()
        return self

    def __exit__(self, *exc):
        self.real.release()
        if not self._busy:
            self._run(self.after.get(self._current))
        return False


import threading  # noqa: E402  (kept next to its only users)


def _scenario(branch, monkeypatch):
    older = email_mod.QuotaPause("monthly", None, "operator-required")
    email_mod._replace_local(older)
    seq = email_mod.pause_for_quota("monthly").seq if branch == "stored" else 0
    email_mod._replace_local(older)  # pause_for_quota may have merged into it
    return older, seq


@pytest.mark.parametrize("qualified", [False, True], ids=["unqualified", "qualified"])
@pytest.mark.parametrize("branch", ["stored", "local-only"])
def test_a_newer_local_pause_arriving_at_any_boundary_of_a_clear_is_never_discarded(branch, qualified, monkeypatch):
    real_lock = email_mod._local_lock
    newer = email_mod.QuotaPause("unknown", None, "operator-required")

    def run(boundary):
        older, seq = _scenario(branch, monkeypatch)
        lock = HookedLock()
        monkeypatch.setattr(email_mod, "_local_lock", lock)
        if boundary is not None:
            where, k = boundary
            (lock.before if where == "before" else lock.after)[k] = lambda: email_mod._merge_local(newer)
        try:
            email_mod.clear_quota_pause(expected_seq=seq if qualified else None)
        finally:
            monkeypatch.setattr(email_mod, "_local_lock", real_lock)
        survived = email_mod._get_local()
        email_mod._replace_local(None)
        try:
            email_mod.clear_quota_pause()  # reset the shared state for the next run
        except email_mod.BreakerStoreError:
            pass
        return lock.count, survived

    acquisitions, after_clear = run(None)
    assert acquisitions >= 1
    boundaries = [("after", k) for k in range(1, acquisitions + 1)] + [("before", k) for k in range(2, acquisitions + 1)]
    for boundary in boundaries:  # the newer pause lands right after the clear took its snapshot, and at every later lock boundary
        _count, survived = run(boundary)
        assert survived is newer, f"{branch}/{'qualified' if qualified else 'unqualified'}: a newer pause was discarded at {boundary}"
    if not qualified:
        assert after_clear is None  # and without a newer pause the unqualified clear does clear what it inspected


def test_the_compare_and_clear_is_one_atomic_step_a_second_thread_cannot_get_between_check_and_assignment(monkeypatch):
    """The lock is HELD from the identity check to the assignment: a thread that wants to set a newer pause at that moment has to wait,
    and ends up setting it AFTER the clear, so it survives."""
    older = email_mod.QuotaPause("monthly", None, "operator-required")
    newer = email_mod.QuotaPause("unknown", None, "operator-required")
    email_mod._replace_local(older)
    entered, order = threading.Event(), []

    class Slow(threading.Thread):
        def run(self):
            entered.wait(5)
            email_mod._merge_local(newer)  # tries to set the newer pause while the clear is between "check" and "assign"
            order.append("newer set")

    class SlowAssign:
        """Wraps the lock so the clear pauses INSIDE the critical section, after the check and before the assignment."""

        def __init__(self):
            self.real = threading.Lock()

        def __enter__(self):
            self.real.acquire()
            return self

        def __exit__(self, *exc):
            self.real.release()
            return False

    lock = SlowAssign()
    monkeypatch.setattr(email_mod, "_local_lock", lock)
    worker = Slow()
    worker.start()
    with lock.real:  # the clear is "inside" its critical section: hold the lock, let the other thread try
        entered.set()
        worker.join(timeout=0.5)
        assert worker.is_alive() and order == []  # it could not get in between
    worker.join(timeout=5)
    assert order == ["newer set"] and email_mod._get_local() is newer


def test_check_breaker_publishing_a_local_pause_never_erases_a_newer_one(monkeypatch):
    older = email_mod.QuotaPause("monthly", None, "operator-required")
    newer = email_mod.QuotaPause("unknown", None, "operator-required")
    email_mod._replace_local(older)
    lock = HookedLock()
    monkeypatch.setattr(email_mod, "_local_lock", lock)
    lock.before[2] = lambda: email_mod._merge_local(newer)  # a newer pause lands after the older one was published, before it is dropped
    state = check_breaker(for_sending=True)
    assert state.pause is not None and email_mod._get_local() is newer
    assert email_mod._store_read() is not None  # the older pause WAS published


def test_a_second_emergency_pause_never_weakens_the_first(monkeypatch):
    email_mod._replace_local(None)
    soon = datetime.now(timezone.utc) + timedelta(hours=1)
    later = datetime.now(timezone.utc) + timedelta(hours=9)
    monthly = email_mod.QuotaPause("monthly", None, "operator-required")
    assert email_mod._merge_local(email_mod.QuotaPause("daily", soon, "convention")).until == soon
    assert email_mod._merge_local(email_mod.QuotaPause("daily", later, "convention")).until == later
    assert email_mod._merge_local(email_mod.QuotaPause("daily", soon, "convention")).until == later  # not shortened
    assert email_mod._merge_local(monthly) is monthly
    assert email_mod._merge_local(email_mod.QuotaPause("daily", soon, "convention")) is monthly  # a daily pause cannot replace a monthly one


def test_only_the_lock_helpers_touch_the_local_pause_variable():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(email_mod))
    allowed = {"_get_local", "_merge_local", "_clear_local_if", "_replace_local"}
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name not in allowed:
            if any(isinstance(n, ast.Name) and n.id == "_local" + "_pause" for n in ast.walk(node)):
                offenders.append(node.name)
    assert offenders == [], f"these functions touch _local_pause without the lock helpers: {offenders}"


def test_every_path_that_holds_a_local_pause_goes_through_the_lock(monkeypatch):
    lock = HookedLock()
    monkeypatch.setattr(email_mod, "_local_lock", lock)
    email_mod._replace_local(None)
    monkeypatch.setattr(email_mod, "_PERSIST_DELAY", 0)
    real_write = email_mod._store_write

    def refuse(*a, **k):
        raise email_mod.BreakerStoreError("x")

    monkeypatch.setattr(email_mod, "_store_write", refuse)
    before = lock.count
    email_mod.pause_for_quota("monthly")  # holds the pause locally
    assert lock.count > before
    before = lock.count
    check_breaker()
    assert lock.count > before  # reading it
    monkeypatch.setattr(email_mod, "_store_write", real_write)
    before = lock.count
    email_mod.clear_quota_pause()
    assert lock.count > before  # clearing it


# ------------------------------------------------------------------ 15. recipient identifiers are keyed (HMAC), not a plain hash

def _recorded_keys(org):
    return _rows(org)[0].payload.get("_delivery", {}).get("sent", [])


def test_recipient_identifiers_are_keyed_not_a_guessable_plain_hash(monkeypatch, resend_configured, clean_outbox_table):
    import hashlib

    org, (a, b, c) = _team("hm")
    monkeypatch.setattr(on, "send_email_result", PerAddress())
    _send()
    recorded = _recorded_keys(org)
    assert len(recorded) == 3 and all(k.startswith("h2:") and len(k) == 27 for k in recorded)
    for address in (a, b, c):
        plain = hashlib.sha256(address.encode()).hexdigest()
        assert not any(plain[:16] in k or plain in k for k in recorded)  # confirming a guessed address by hashing it no longer works
    assert on._addr_key(a) in recorded and on._addr_key(a) != on._addr_key(b)


def test_the_key_depends_on_the_server_secret_and_not_on_case_or_spaces(monkeypatch):
    settings = get_settings()
    first = on._addr_key("Person@Example.test ")
    assert first == on._addr_key("person@example.test")
    monkeypatch.setattr(settings, "APP_SECRET_KEY", "x" * 40)
    assert on._addr_key("person@example.test") != first
    assert on._recipient_key("k" * 40, "person@example.test") != on._recipient_key("k" * 40, "other@example.test")


def _first_pass_reaches_only_the_first_recipient(monkeypatch):
    org, (a, b, c) = _team("rot")
    quota = EmailResult(EmailOutcome.QUOTA, "SMTPDataError 554 daily quota", retry_after=datetime.now(timezone.utc) + timedelta(hours=1), quota_kind="daily")
    first = PerAddress(**{b: quota})
    monkeypatch.setattr(on, "send_email_result", first)
    _send()
    assert first.calls == [a, b] and len(_recorded_keys(org)) == 1
    return org, (a, b, c)


def test_after_a_secret_rotation_the_previous_secret_keeps_already_emailed_recipients_from_being_emailed_again(monkeypatch, resend_configured, clean_outbox_table):
    settings = get_settings()
    old_secret = settings.APP_SECRET_KEY
    org, (a, b, c) = _first_pass_reaches_only_the_first_recipient(monkeypatch)
    monkeypatch.setattr(settings, "APP_SECRET_KEY", "rotated-" + "y" * 40)
    monkeypatch.setattr(settings, "APP_SECRET_KEY_PREVIOUS", f"older-{'z' * 40}, {old_secret}")
    later = PerAddress()
    monkeypatch.setattr(on, "send_email_result", later)
    assert _send(now=datetime.now(timezone.utc) + timedelta(hours=2))["sent"] == 1
    assert later.calls == [b, c]  # a was recognised under the previous secret: not emailed again
    assert _rows(org)[0].status == "sent"


def test_a_rotation_without_the_previous_secret_is_documented_not_silent(monkeypatch, resend_configured, clean_outbox_table):
    """Without APP_SECRET_KEY_PREVIOUS an already-emailed recipient cannot be recognised and gets ONE duplicate; nothing is lost or stuck."""
    settings = get_settings()
    org, (a, b, c) = _first_pass_reaches_only_the_first_recipient(monkeypatch)
    monkeypatch.setattr(settings, "APP_SECRET_KEY", "rotated-" + "y" * 40)
    monkeypatch.setattr(settings, "APP_SECRET_KEY_PREVIOUS", "")
    later = PerAddress()
    monkeypatch.setattr(on, "send_email_result", later)
    assert _send(now=datetime.now(timezone.utc) + timedelta(hours=2))["sent"] == 1
    assert later.calls == [a, b, c] and _rows(org)[0].status == "sent"


def test_recipients_recorded_with_the_earlier_unkeyed_format_are_still_recognised_and_never_rewritten(monkeypatch, resend_configured, clean_outbox_table):
    import hashlib

    org, (a, b, c) = _team("lg")
    db = SessionLocal()
    try:
        row = db.query(on.OwnerNotification).filter(on.OwnerNotification.organization_id == uuid.UUID(org)).first()
        row.payload = {**(row.payload or {}), "_delivery": {"sent": [hashlib.sha256(a.encode()).hexdigest()[:16]], "refused": []}}
        db.commit()
    finally:
        db.close()
    seen = PerAddress()
    monkeypatch.setattr(on, "send_email_result", seen)
    _send()
    assert seen.calls == [b, c]  # a was already recorded in the old format
    recorded = _recorded_keys(org)
    assert len(recorded) == 3 and sum(1 for k in recorded if k.startswith("h2:")) == 2  # only new entries are keyed


# ------------------------------------------------------------------ 16. the effective pause is always the STRICTER of the recorded and the local one

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)


def _record(kind, until, now=NOW):
    """Writes a pause into PostgreSQL exactly as another process would, and returns it (with its sequence number)."""
    pause = email_mod.QuotaPause(kind, until, "operator-required" if until is None else "convention")
    return replace_seq(pause, email_mod._record_pause(pause, now).seq)


def replace_seq(pause, seq):
    import dataclasses

    return dataclasses.replace(pause, seq=seq)


def _local(monkeypatch, kind, until):
    held = email_mod.QuotaPause(kind, until, "operator-required" if until is None else "convention")
    email_mod._replace_local(held)
    return held


def _events_since(before):
    return _events()[before:]


def test_the_merge_rule_orders_pauses_by_how_long_they_keep_sending_stopped():
    q = email_mod.QuotaPause
    never = q("monthly", None, "operator-required")
    soon, later = q("daily", NOW + HOUR, "convention"), q("daily", NOW + 5 * HOUR, "convention")
    assert email_mod._compare(never, soon) == 1 and email_mod._compare(soon, never) == -1
    assert email_mod._compare(later, soon) == 1 and email_mod._compare(soon, later) == -1
    assert email_mod._compare(never, q("unknown", None, "operator-required")) == 0
    assert email_mod._compare(soon, q("daily", NOW + HOUR, "server")) == 0
    assert email_mod._stricter(soon, never) is never and email_mod._stricter(never, soon) is never and email_mod._stricter(soon, later) is later
    assert email_mod._stricter(soon, q("daily", NOW + HOUR, "server")) is soon  # a tie goes to the first argument, the newer pause


@pytest.mark.parametrize("reading", ["read", "for_sending"])
def test_a_local_monthly_pause_beats_a_recorded_daily_pause_and_is_then_published(monkeypatch, reading):
    stored = _record("daily", NOW + 6 * HOUR)
    local = _local(monkeypatch, "monthly", None)
    state = check_breaker(NOW, for_sending=(reading == "for_sending"))
    assert state.available and state.pause.kind == "monthly" and state.pause.operator_required  # NOT the daily pause the old code returned
    assert state.pause.seq > stored.seq and email_mod._get_local() is None  # published as the newest event, then dropped
    _another_process(monkeypatch)  # a different process now sees the stricter pause too, including after the daily one would have ended
    for later in (NOW, NOW + 7 * HOUR, NOW + 30 * 24 * HOUR):
        seen = check_breaker(later).pause
        assert seen is not None and seen.kind == "monthly" and seen.operator_required
    assert local.operator_required


def test_a_local_monthly_pause_is_kept_and_still_applies_while_the_database_refuses_to_publish_it(monkeypatch):
    stored = _record("daily", NOW + 6 * HOUR)
    local = _local(monkeypatch, "monthly", None)
    real_write = email_mod._store_write

    def refuse(*a, **k):
        raise email_mod.BreakerStoreError("ReadOnlySqlTransaction")

    monkeypatch.setattr(email_mod, "_store_write", refuse)
    before = _event_count()
    state = check_breaker(NOW)
    assert state.pause is local and email_mod._get_local() is local  # stricter than the recorded daily pause, and not lost
    assert check_breaker(NOW + 7 * HOUR).pause is local  # still in force after the recorded daily pause has expired
    assert _event_count() == before and email_mod._store_read().seq == stored.seq
    monkeypatch.setattr(email_mod, "_store_write", real_write)  # the database recovers
    published = check_breaker(NOW + 7 * HOUR).pause
    assert published.kind == "monthly" and published.seq > stored.seq and email_mod._get_local() is None
    _another_process(monkeypatch)
    assert check_breaker(NOW + 30 * 24 * HOUR).pause.operator_required


def test_a_local_pause_with_a_later_reset_beats_a_recorded_pause_with_an_earlier_one(monkeypatch):
    stored = _record("daily", NOW + 2 * HOUR)
    _local(monkeypatch, "daily", NOW + 9 * HOUR)
    state = check_breaker(NOW)
    assert state.pause.until == NOW + 9 * HOUR and state.pause.seq > stored.seq and email_mod._get_local() is None
    _another_process(monkeypatch)
    assert check_breaker(NOW + 3 * HOUR).pause is not None and check_breaker(NOW + 3 * HOUR).pause.until == NOW + 9 * HOUR  # past the earlier reset
    assert check_breaker(NOW + 10 * HOUR).pause is None


def test_a_recorded_pause_with_a_later_reset_beats_a_local_pause_with_an_earlier_one_and_nothing_is_written(monkeypatch):
    stored = _record("daily", NOW + 9 * HOUR)
    _local(monkeypatch, "daily", NOW + 2 * HOUR)
    before = _event_count()
    state = check_breaker(NOW)
    assert state.pause.seq == stored.seq and state.pause.until == NOW + 9 * HOUR
    assert _event_count() == before and email_mod._get_local() is None  # already covered by the recorded pause: nothing written, the local buffer is dropped


@pytest.mark.parametrize("until", [None, NOW + 4 * HOUR], ids=["both-operator-required", "equal-reset-time"])
def test_a_local_pause_with_the_same_expiry_as_the_recorded_one_defers_to_the_recorded_one(monkeypatch, until):
    stored = _record("monthly" if until is None else "daily", until)
    _local(monkeypatch, "monthly" if until is None else "daily", until)
    before = _event_count()
    state = check_breaker(NOW, for_sending=True)
    assert state.pause.seq == stored.seq  # a tie goes to the recorded pause
    assert _event_count() == before and email_mod._get_local() is None  # nothing duplicated; the recorded pause already covers it, so the local buffer is dropped


def test_a_weaker_new_quota_reply_never_weakens_a_stricter_local_pause_even_when_the_database_write_succeeds(monkeypatch):
    local = _local(monkeypatch, "monthly", None)
    before = _event_count()
    effective = email_mod.pause_for_quota("daily", now=NOW)  # the provider now says "daily quota": weaker than what this process holds
    assert effective.kind == "monthly" and effective.operator_required  # the stricter pause is what is returned...
    new = _events_since(before)
    assert [e[1] for e in new] == [email_mod.PAUSE_ACTION]  # ...only the monthly pause is recorded; the weaker daily one is covered by it and never becomes the newest event
    assert email_mod._store_read().operator_required and email_mod._get_local() is None
    _another_process(monkeypatch)
    assert check_breaker(NOW + 3 * 24 * HOUR).pause.operator_required and local.operator_required


def test_a_weaker_new_quota_reply_never_weakens_a_stricter_local_pause_when_the_database_write_fails(monkeypatch):
    local = _local(monkeypatch, "monthly", None)
    monkeypatch.setattr(email_mod, "_PERSIST_DELAY", 0)
    real_write = email_mod._store_write

    def refuse(*a, **k):
        raise email_mod.BreakerStoreError("ReadOnlySqlTransaction")

    monkeypatch.setattr(email_mod, "_store_write", refuse)
    effective = email_mod.pause_for_quota("daily", now=NOW)
    assert effective is local and email_mod._get_local() is local  # still the monthly pause, not replaced by the daily one
    monkeypatch.setattr(email_mod, "_store_write", real_write)
    assert check_breaker(NOW + 7 * HOUR).pause.operator_required  # recovery publishes the strict one


def test_a_stronger_new_quota_reply_replaces_a_weaker_local_pause(monkeypatch):
    _local(monkeypatch, "daily", NOW + 2 * HOUR)
    effective = email_mod.pause_for_quota("monthly", now=NOW)
    assert effective.operator_required and effective.seq > 0
    assert email_mod._get_local() is None  # the weaker local pause is covered by the stricter recorded one and dropped
    assert check_breaker(NOW).pause.operator_required  # and the stricter recorded one governs


def test_the_send_path_reports_and_enforces_the_effective_pause(quota_transport, monkeypatch):
    """send_email_result: a recorded daily pause plus a stricter local monthly pause. The provider is not contacted and the answer is the monthly pause."""
    _record("daily", datetime.now(timezone.utc) + 6 * HOUR)
    _local(monkeypatch, "monthly", None)
    result = email_mod.send_email_result("a@example.test", "s", "b")
    assert result.outcome is EmailOutcome.QUOTA and result.quota_kind == "monthly" and result.retry_after is None
    assert quota_transport.constructed == 0
    _another_process(monkeypatch)
    assert email_mod.send_email_result("a@example.test", "s", "b").quota_kind == "monthly"  # published: every process agrees


def test_a_quota_reply_in_flight_with_a_stricter_local_pause_reports_the_effective_pause(quota_transport, monkeypatch):
    local = _local(monkeypatch, "monthly", None)

    def daily(self, msg):
        raise smtplib.SMTPDataError(554, b"Daily quota exceeded")

    monkeypatch.setattr(quota_transport, "send_message", daily)
    monkeypatch.setattr(email_mod, "check_breaker", lambda *a, **k: email_mod.BreakerState(True, None))  # the send passed its gate before the local pause existed
    result = email_mod.send_email_result("a@example.test", "s", "b")
    assert (result.outcome, result.quota_kind, result.retry_after) == (EmailOutcome.QUOTA, "monthly", None)  # not the weaker daily answer
    assert local.operator_required


def test_no_test_or_fixture_assigns_or_reads_the_local_pause_variable_directly():
    """Invariant: nothing bypasses the lock. Tests and fixtures use _get_local/_replace_local, never the variable."""
    import pathlib
    import re

    name = "_local" + "_pause"
    pattern = re.compile(r"(\.|\")" + name + r"\b")
    offenders = [p.name for p in pathlib.Path(__file__).parent.glob("*.py") if pattern.search(p.read_text())]
    assert offenders == []
