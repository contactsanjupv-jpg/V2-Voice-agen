"""Channel policy: email ONLY for password recovery and security alerts. Everything else is in-app, and the two channels are independent."""
import pathlib
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core import email as email_mod
from app.db.base import SessionLocal
from app.db.models.notifications import Notification, OwnerNotification
from app.db.models.tenancy import OrganizationMember
from app.main import app
from app.services import notifications as svc
from app.services import owner_notifications as on
from tests.billing_helpers import new_org
from tests.test_inbound_call_resolution import _retell_key  # noqa: F401  (autouse: signs the Retell webhook the lead scenario posts)
from tests.test_owner_notifications import LEAD, _analyzed, _enforce, _live_org, _paused_setup

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
ALL_KINDS = [on.KIND_LEAD, on.KIND_SERVICE_PAUSED, on.KIND_LOW_CREDITS, on.KIND_CREDITS_EMPTY, on.KIND_PAYMENT_FAILED]


@pytest.fixture(autouse=True)
def _real_policy(monkeypatch):
    """The production configuration: owner notifications enabled, nothing email-eligible."""
    monkeypatch.setattr(get_settings(), "OWNER_NOTIFICATIONS_ENABLED", True)
    assert on.EMAIL_KINDS == frozenset()  # not patched here (other files patch it for their machinery tests, per test)


def _counts(org):
    db = SessionLocal()
    try:
        oid = uuid.UUID(org)
        return (
            db.query(Notification).filter(Notification.organization_id == oid).count(),
            db.query(OwnerNotification).filter(OwnerNotification.organization_id == oid).count(),
        )
    finally:
        db.close()


def _explode(*_a, **_k):
    raise AssertionError("an email was attempted for an operational notification")


# ------------------------------------------------------------------ nothing operational is ever emailed
def test_the_production_policy_has_no_email_eligible_operational_kind():
    assert on.EMAIL_KINDS == frozenset()
    assert set(ALL_KINDS) <= set(svc.CATALOGUE) and not (set(svc.CATALOGUE) & on.EMAIL_KINDS)  # every kind is in-app; none is email-eligible


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_enqueue_creates_the_in_app_notification_and_queues_no_email(kind, monkeypatch, outbox):
    _c, org = new_org("policy")
    monkeypatch.setattr(on, "send_email_result", _explode)
    monkeypatch.setattr(email_mod, "send_email_result", _explode)
    db = SessionLocal()
    try:
        assert on.enqueue(db, uuid.UUID(org), kind, "k1", {"lead_id": str(uuid.uuid4()), "reason": "credits_exhausted"}) is True
        db.commit()
        on.send_due_notifications(db)  # the worker pass has nothing to send
    finally:
        db.close()
    assert _counts(org) == (1, 0) and outbox.sent == []


def test_a_whole_worker_pass_over_every_operational_event_sends_no_email(monkeypatch, outbox):
    monkeypatch.setattr(on, "send_email_result", _explode)
    org, agent, number = _live_org()
    _analyzed(org, agent, number, LEAD)  # a lead
    porg, provider = _paused_setup(monkeypatch)
    _enforce(porg, provider)  # a pause
    for o in (org, porg):
        assert _counts(o)[0] >= 1 and _counts(o)[1] == 0
    db = SessionLocal()
    try:
        on.send_due_notifications(db)
        on.notify_credit_levels(db)
    finally:
        db.close()
    assert outbox.sent == []


# ------------------------------------------------------------------ the channels are independent
def test_in_app_creation_works_when_the_email_service_is_down_or_disabled(monkeypatch):
    _c, org = new_org("indep")
    monkeypatch.setattr(on, "send_email_result", _explode)
    monkeypatch.setattr(on, "check_breaker", lambda *a, **k: (_ for _ in ()).throw(email_mod.BreakerStoreError("db down")))
    monkeypatch.setattr(get_settings(), "OWNER_NOTIFICATIONS_ENABLED", False)  # even the old email switch cannot turn off in-app
    db = SessionLocal()
    try:
        assert on.enqueue(db, uuid.UUID(org), "payment_failed", "down", {}) is True
        db.commit()
    finally:
        db.close()
    assert _counts(org) == (1, 0)


def test_a_failure_in_the_email_queue_cannot_block_or_undo_the_in_app_notification(monkeypatch):
    _c, org = new_org("emailfail")
    monkeypatch.setattr(on, "EMAIL_KINDS", frozenset({"payment_failed"}))  # even if a kind WERE email-eligible...

    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("outbox table unavailable")

    monkeypatch.setattr(on, "OwnerNotification", Boom)
    db = SessionLocal()
    try:
        assert on.enqueue(db, uuid.UUID(org), "payment_failed", "k", {}) is True  # ...its failure is swallowed in its own savepoint
        db.commit()
    finally:
        db.close()
    assert _counts(org) == (1, 0)


def test_in_app_creation_never_imports_or_calls_the_email_layer():
    import ast

    tree = ast.parse((APP / "services" / "notifications.py").read_text())
    imported = [n.module if isinstance(n, ast.ImportFrom) else a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in (n.names if isinstance(n, ast.Import) else [n])]
    imported = [m for m in imported if m]
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not [m for m in imported if re.search(r"core\.email|smtplib|owner_notifications", m)]
    assert not {"send_email", "send_email_result", "check_breaker"} & names


# ------------------------------------------------------------------ rows queued before the policy
def test_a_pre_policy_outbox_row_becomes_an_in_app_notification_and_is_never_emailed(monkeypatch, outbox):
    monkeypatch.setattr(on, "send_email_result", _explode)
    _c, org = new_org("legacy")
    old = datetime.now(timezone.utc) - timedelta(hours=5)
    db = SessionLocal()
    try:
        db.add(OwnerNotification(organization_id=uuid.UUID(org), kind="payment_failed", dedupe_key="legacy-1", payload={"subscription_id": "sub_x", "_claim": "t"}, status="pending", attempts=0, created_at=old))
        db.commit()
        result = on.send_due_notifications(db)
        again = on.send_due_notifications(db)
    finally:
        db.close()
    db = SessionLocal()
    try:
        (legacy,) = db.query(OwnerNotification).filter(OwnerNotification.organization_id == uuid.UUID(org)).all()
        (item,) = db.query(Notification).filter(Notification.organization_id == uuid.UUID(org)).all()
    finally:
        db.close()
    assert legacy.status == "skipped" and "in-app only" in legacy.last_error and outbox.sent == []
    assert item.kind == "payment_failed" and item.created_at == old and "_claim" not in item.payload  # the original time, no internal keys
    assert result is not None and again is not None


# ------------------------------------------------------------------ the two permitted emails still work
def test_password_recovery_and_the_password_changed_security_alert_are_still_emailed(monkeypatch):
    sent = []
    monkeypatch.setattr("app.api.v1.account.send_email", lambda to, subject, body: sent.append((to, subject, body)) or True)
    tag = uuid.uuid4().hex[:8]
    email = f"policy-{tag}@policy-test.com"
    from tests.test_tenant_isolation import _signup

    _signup(email, f"Policy {tag}")
    anon = TestClient(app)
    assert anon.post("/api/v1/auth/password-reset/request", json={"email": email}).status_code == 204
    (to, subject, body) = sent[-1]
    assert to == email and subject == "Reset your Atla password"
    token = re.search(r"token=([\w-]+)", body).group(1)
    assert anon.post("/api/v1/auth/password-reset/confirm", json={"token": token, "new_password": "a-brand-new-passphrase"}).status_code == 204
    assert sent[-1][1] == "Your Atla password was changed"  # the security alert


# ------------------------------------------------------------------ nothing else can send, now or by accident
def test_only_the_account_endpoints_send_email_to_customers():
    """Modules that call the mail layer: the account and sign-in endpoints (password recovery, security alerts), the outbox machinery
    (no email-eligible kind), and the operator's test-email command."""
    callers = sorted(str(f.relative_to(APP)) for f in APP.rglob("*.py") if re.search(r"from app\.core\.email import[^\n]*\bsend_email", f.read_text()))
    assert callers == ["api/v1/account.py", "services/owner_notifications.py", "workers/send_test_email.py"]
    deliverers = sorted(str(f.relative_to(APP)) for f in APP.rglob("*.py") if re.search(r"\b_deliver\b", f.read_text()) and f.name != "account.py")
    assert deliverers == ["api/v1/google_auth.py"]  # the Google link verification / security alerts


def test_there_is_no_report_digest_announcement_or_product_update_email_code():
    forbidden = re.compile(r"def\s+\w*(daily_report|daily_digest|email_digest|weekly_digest|announcement|product_update|feature_update|newsletter|marketing|broadcast)\w*\s*\(", re.I)
    offenders = [f"{f.relative_to(APP)}: {m.group(0)}" for f in APP.rglob("*.py") for m in forbidden.finditer(f.read_text())]
    assert offenders == []
    # A module named like a report/digest may exist (the in-app daily report does) but must contain no way to send email: no
    # sending helper, no SMTP/Resend, no email-queue/policy hook, no import of the email layer. (Prose saying "no email" is fine.)
    sends = re.compile(r"send_email|smtplib|resend|owner_notifications|EMAIL_KINDS|app\.core\.email|import\s+email", re.I)
    assert not [f.name for f in APP.rglob("*.py") if re.search(r"daily|digest|newsletter|announce", f.name, re.I) and sends.search(f.read_text()) and f.name != "email.py"]


def test_every_member_of_an_audience_is_resolved_from_the_database_not_from_email_addresses():
    """The in-app audience is user ids (rows per recipient), so no address is ever needed to notify someone."""
    _c, org = new_org("aud")
    db = SessionLocal()
    try:
        svc.create(db, uuid.UUID(org), "payment_failed", "aud")
        db.commit()
        ids = {n.recipient_user_id for n in db.query(Notification).filter(Notification.organization_id == uuid.UUID(org))}
        members = {m.user_id for m in db.query(OrganizationMember).filter(OrganizationMember.organization_id == uuid.UUID(org))}
    finally:
        db.close()
    assert ids == members
