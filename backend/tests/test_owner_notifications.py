"""
Level 8A: owner notifications. The business owner must be told (by email) about a new lead, a paused receptionist,
low/empty credits and a failed payment, exactly once per cause, without ever making a webhook or the call pipeline wait
for SMTP, and without sending a message that is no longer true.

Real Postgres and the real HTTP/webhook layer; only the SMTP hand-off (send_email) is replaced by a recorder.
"""
import uuid
from datetime import datetime, timedelta, timezone
from email import message_from_string
from email.message import EmailMessage
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.billing import Subscription
from app.db.models.calls import Call
from app.db.models.crm import Lead
from app.db.models.notifications import OwnerNotification
from app.db.models.tenancy import OrganizationMember, OrgRole, User
from app.db.models.voice_agent import Agent, AgentStatus
from app.services import monitoring, owner_notifications as on
from app.services.entitlement import enforce_entitlements
from app.services.plans import PLANS, Plan
from app.services.subscription_sync import sync_subscription
from tests.billing_helpers import add_usage, new_org, subscribe
from tests.fakes import FakePhone
from tests.test_call_pipeline import _seed
from tests.test_inbound_call_resolution import _cid, _event, _post, _retell_key  # noqa: F401  (_retell_key: autouse)

MIN = 60
FRONTEND = "https://app.example.test"


MAILBOX_REF: list = [None]


class Mailbox:
    """Stands in for core.email.send_email: records messages, can refuse them."""

    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []
        self.refuse: set[str] | bool = False

    def __call__(self, to, subject, body):
        if self.refuse is True or (isinstance(self.refuse, set) and to in self.refuse):
            return False
        self.sent.append((to, subject, body))
        return True


@pytest.fixture(autouse=True)
def mailbox(monkeypatch):
    box = Mailbox()
    MAILBOX_REF[0] = box
    monkeypatch.setattr(on, "send_email", box)
    monkeypatch.setattr(get_settings(), "FRONTEND_URL", FRONTEND)
    monkeypatch.setattr(get_settings(), "OWNER_NOTIFICATIONS_ENABLED", True)
    _wipe()
    yield box
    _wipe()


def _wipe():
    db = SessionLocal()
    try:
        db.query(OwnerNotification).delete()
        db.commit()
    finally:
        db.close()


def _rows(org_id, kind=None):
    db = SessionLocal()
    try:
        q = db.query(OwnerNotification).filter(OwnerNotification.organization_id == uuid.UUID(org_id))
        if kind:
            q = q.filter(OwnerNotification.kind == kind)
        return q.order_by(OwnerNotification.created_at).all()
    finally:
        db.close()


def _send(now=None):
    db = SessionLocal()
    try:
        return on.send_due_notifications(db, now=now)
    finally:
        db.close()


def _mail_for(org_id):
    """The messages this test's organization received (the database is shared with other tests' organizations)."""
    mine = {_owner_email(org_id)} | {e for e in _member_emails(org_id)}
    return [m for m in MAILBOX_REF[0].sent if m[0] in mine]


def _member_emails(org_id):
    db = SessionLocal()
    try:
        return [
            r[0]
            for r in db.query(User.email)
            .join(OrganizationMember, OrganizationMember.user_id == User.id)
            .filter(OrganizationMember.organization_id == uuid.UUID(org_id))
            .all()
        ]
    finally:
        db.close()


def _owner_email(org_id):
    db = SessionLocal()
    try:
        return (
            db.query(User.email)
            .join(OrganizationMember, OrganizationMember.user_id == User.id)
            .filter(OrganizationMember.organization_id == uuid.UUID(org_id), OrganizationMember.role == OrgRole.owner)
            .one()[0]
        )
    finally:
        db.close()


def _add_member(org_id, role):
    db = SessionLocal()
    try:
        user = User(email=f"{role.value}-{uuid.uuid4().hex[:8]}@notify-test.com", password_hash="x")
        db.add(user)
        db.flush()
        db.add(OrganizationMember(organization_id=uuid.UUID(org_id), user_id=user.id, role=role))
        db.commit()
        return user.email
    finally:
        db.close()


def _live_org():
    """An organization with an owner, a live receptionist and a live number. Returns (org_id, retell_agent_id, number)."""
    _c, org = new_org("notify")
    retell_agent_id, number = _seed(org)
    return org, retell_agent_id, number


def _analyzed(org, retell_agent_id, number, custom, **over):
    cid = _cid()
    analysis = {"call_summary": "Burst pipe in the kitchen.", "custom_analysis_data": custom}
    resp = _post(_event("call_analyzed", cid, number, {"retell_agent_id": retell_agent_id}, call_status="ended",
                        call_analysis=analysis, **over))
    assert resp.status_code == 204
    return cid


LEAD = {"captured_lead": True, "caller_name": "Dana Plumber", "callback_number": "+15551230000", "reason": "Burst pipe"}


def _paused_setup(monkeypatch):
    """A live org whose included minutes are all spent, 39 minutes over a 30-minute grace (so it must be paused)."""
    monkeypatch.setitem(PLANS, "starter", Plan("starter", "Starter", 1, PLANS["starter"].features, 1))
    monkeypatch.setattr(get_settings(), "OVERAGE_GRACE_MINUTES", 30)
    org, _agent, number = _live_org()
    subscribe(org)
    add_usage(org, 40 * MIN)
    provider = FakePhone()
    provider.numbers[number] = {"nickname": "n", "agent": "agent_x"}
    return org, provider


def _enforce(org, provider, now=None):
    db = SessionLocal()
    try:
        return enforce_entitlements(db, provider, now=now, organization_ids={uuid.UUID(org)})
    finally:
        db.close()


# =========================================================== new lead

def test_a_real_lead_queues_exactly_one_notification_holding_ids_only():
    org, agent, number = _live_org()
    cid = _analyzed(org, agent, number, LEAD)

    (n,) = _rows(org)
    db = SessionLocal()
    try:
        lead = db.query(Lead).join(Call, Lead.call_id == Call.id).filter(Call.retell_call_id == cid).one()
        assert (n.kind, n.status, n.attempts, n.dedupe_key) == ("lead", "pending", 0, str(lead.id))
        assert n.payload == {"lead_id": str(lead.id)}  # no caller name, number or reason copied into the outbox
    finally:
        db.close()


def test_redelivered_and_repeated_ingestion_never_queues_a_second_notification():
    org, agent, number = _live_org()
    cid = _cid()
    event = _event("call_analyzed", cid, number, {"retell_agent_id": agent}, call_status="ended",
                   call_analysis={"call_summary": "x", "custom_analysis_data": LEAD})
    assert _post(event).status_code == 204
    assert _post(event).status_code == 204  # Paddle/Retell-style redelivery: idempotent at the webhook

    assert len(_rows(org, "lead")) == 1
    db = SessionLocal()
    try:
        # ... and the enqueue itself is idempotent per (org, kind, key): a retry of the same cause is a no-op.
        assert on.enqueue(db, uuid.UUID(org), "lead", _rows(org)[0].dedupe_key, {"lead_id": "x"}) is False
        db.commit()
    finally:
        db.close()
    assert len(_rows(org, "lead")) == 1


def test_a_browser_test_call_lead_does_not_notify_the_owner():
    org, agent, number = _live_org()
    cid = _cid()
    _post(_event("call_analyzed", cid, None, {"retell_agent_id": agent}, call_type="web_call", direction=None,
                 from_number=None, to_number=None, call_status="ended",
                 call_analysis={"call_summary": "x", "custom_analysis_data": LEAD}))

    db = SessionLocal()
    try:
        assert db.query(Lead).filter(Lead.organization_id == uuid.UUID(org)).one().source == "test_call"
    finally:
        db.close()
    assert _rows(org) == []


def test_a_call_that_captured_no_lead_notifies_nobody():
    org, agent, number = _live_org()
    _analyzed(org, agent, number, {"captured_lead": False})

    assert _rows(org) == []


def test_a_failed_ingest_leaves_no_notification_behind(monkeypatch):
    """The notification is part of the same savepoint as the lead: if the event fails afterwards and is retried,
    nothing is left over and nothing is duplicated."""
    from app.services import call_ingestion

    real = call_ingestion._upsert_lead
    state = {"fail": True}

    def lead_then_boom(db, call, pcall):
        real(db, call, pcall)
        if state["fail"]:
            raise RuntimeError("boom after the lead was written")

    monkeypatch.setattr(call_ingestion, "_upsert_lead", lead_then_boom)
    org, agent, number = _live_org()
    cid = _analyzed(org, agent, number, LEAD)

    db = SessionLocal()
    try:
        assert db.query(Lead).filter(Lead.organization_id == uuid.UUID(org)).count() == 0
    finally:
        db.close()
    assert _rows(org) == []

    state["fail"] = False
    from app.db.models.platform import WebhookEvent
    from app.workers import retell_events

    db = SessionLocal()
    try:
        ev = db.query(WebhookEvent).filter(WebhookEvent.external_event_id == f"{cid}:call_analyzed").one()
        ev.status, ev.next_attempt_at = "failed", datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()
    retell_events.retry_due_events()
    assert len(_rows(org, "lead")) == 1


def test_lead_email_goes_to_owners_and_admins_not_members_and_is_sent_once(mailbox):
    org, agent, number = _live_org()
    admin = _add_member(org, OrgRole.admin)
    member = _add_member(org, OrgRole.member)
    _analyzed(org, agent, number, LEAD)

    assert _send()["sent"] == 1
    assert {to for to, _s, _b in _mail_for(org)} == {_owner_email(org), admin}
    assert member not in {to for to, _s, _b in mailbox.sent}
    _to, subject, body = _mail_for(org)[0]
    assert subject == "New lead: Dana Plumber"
    for expected in ("Dana Plumber", "+15551230000", "Burst pipe", "Burst pipe in the kitchen.", f"{FRONTEND}/dashboard/leads"):
        assert expected in body
    (n,) = _rows(org)
    assert n.status == "sent" and n.sent_at is not None and n.attempts == 1

    assert _send()["sent"] == 0 and len(_mail_for(org)) == 2  # a second pass sends nothing


def test_lead_text_cannot_break_the_email_headers_or_smuggle_a_recipient(mailbox):
    org, agent, number = _live_org()
    hostile = "Bob\r\nBcc: attacker@evil.test\x1b\u2028Subject: pwned"
    _analyzed(org, agent, number, {**LEAD, "caller_name": hostile, "reason": hostile})
    _send()

    _to, subject, body = _mail_for(org)[0]
    assert not any(ch in subject for ch in "\r\n\x1b\u2028")
    assert "\x1b" not in body and "\r" not in body
    msg = EmailMessage()
    msg["Subject"] = subject  # the real mail library refuses a header containing a newline: this is what send_email does
    msg["To"] = _owner_email(org)
    msg.set_content(body)
    parsed = message_from_string(msg.as_string())
    assert parsed.get_all("Subject") == [subject] and parsed["Bcc"] is None  # one header, no injected recipient


def test_text_helpers_strip_control_characters_including_nul():
    assert on._one_line("a\x00b\r\nc\u2028d", 50) == "ab c d"
    assert on._block("a\x00b\x1bc\nd", 50) == "abc\nd"
    assert len(on._one_line("x" * 500, 80)) == 80


def test_a_lead_notification_for_another_organizations_lead_is_not_sent(mailbox):
    org, agent, number = _live_org()
    other, other_agent, other_number = _live_org()
    _analyzed(other, other_agent, other_number, LEAD)
    other_lead = _rows(other)[0].payload["lead_id"]
    db = SessionLocal()
    try:
        on.enqueue(db, uuid.UUID(org), "lead", "forged", {"lead_id": other_lead})
        db.commit()
    finally:
        db.close()

    _send()

    assert [row.status for row in _rows(org)] == ["skipped"]
    assert all("Dana Plumber" not in body for to, _s, body in mailbox.sent if to == _owner_email(org))


# =========================================================== delivery: retry, partial, nobody, disabled

def test_a_refused_email_is_retried_with_backoff_then_delivered(mailbox):
    org, agent, number = _live_org()
    _analyzed(org, agent, number, LEAD)
    mailbox.refuse = True
    start = datetime.now(timezone.utc)

    assert _send(now=start)["retry"] == 1
    (n,) = _rows(org)
    assert (n.status, n.attempts) == ("pending", 1) and n.next_attempt_at > start and n.last_error

    assert _send(now=start + timedelta(seconds=5)) == {"sent": 0, "skipped": 0, "failed": 0, "retry": 0}  # not due yet
    mailbox.refuse = False
    assert _send(now=start + timedelta(minutes=5))["sent"] == 1
    assert _rows(org)[0].status == "sent"


def test_after_the_last_attempt_the_notification_fails_visibly_and_raises_an_alert(mailbox, monkeypatch):
    monkeypatch.setattr(get_settings(), "OWNER_NOTIFICATION_MAX_ATTEMPTS", 3)
    org, agent, number = _live_org()
    _analyzed(org, agent, number, LEAD)
    mailbox.refuse = True
    now = datetime.now(timezone.utc)
    for i in range(3):
        _send(now=now + timedelta(hours=2 * i))

    (n,) = _rows(org)
    assert (n.status, n.attempts, n.next_attempt_at) == ("failed", 3, None)
    db = SessionLocal()
    try:
        alerts = {a.key: a for a in monitoring.alert_conditions(db)}
    finally:
        db.close()
    assert alerts["owner_notifications_undelivered"].count >= 1
    assert alerts["owner_notifications_undelivered"].severity == "critical"


def test_alert_for_notifications_stuck_pending_and_none_when_healthy(mailbox):
    org, agent, number = _live_org()
    _analyzed(org, agent, number, LEAD)
    db = SessionLocal()
    try:
        assert "owner_notifications_undelivered" not in {a.key for a in monitoring.alert_conditions(db)}
        later = datetime.now(timezone.utc) + timedelta(minutes=monitoring.STUCK_MINUTES + 1)
        assert "owner_notifications_undelivered" in {a.key for a in monitoring.alert_conditions(db, now=later)}
    finally:
        db.close()
    _send()  # delivered: nothing stuck any more
    db = SessionLocal()
    try:
        later = datetime.now(timezone.utc) + timedelta(minutes=monitoring.STUCK_MINUTES + 1)
        assert "owner_notifications_undelivered" not in {a.key for a in monitoring.alert_conditions(db, now=later)}
    finally:
        db.close()


def test_one_working_recipient_is_enough_and_is_not_emailed_twice_on_retry(mailbox):
    org, agent, number = _live_org()
    admin = _add_member(org, OrgRole.admin)
    _analyzed(org, agent, number, LEAD)
    mailbox.refuse = {admin}

    assert _send()["sent"] == 1
    assert [to for to, _s, _b in _mail_for(org)] == [_owner_email(org)]
    _send()
    assert len(_mail_for(org)) == 1


def test_an_error_while_building_the_message_is_recorded_and_retried(mailbox, monkeypatch):
    org, agent, number = _live_org()
    _analyzed(org, agent, number, LEAD)
    real = on.render

    def broken(db, n):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(on, "render", broken)
    assert _send()["retry"] == 1
    (n,) = _rows(org)
    assert n.status == "pending" and n.attempts == 1 and n.last_error.startswith("RuntimeError")

    monkeypatch.setattr(on, "render", real)
    assert _send(now=datetime.now(timezone.utc) + timedelta(minutes=5))["sent"] == 1


def test_an_organization_with_nobody_to_tell_is_skipped_not_retried_forever(mailbox):
    org, agent, number = _live_org()
    _analyzed(org, agent, number, LEAD)
    db = SessionLocal()
    try:
        db.query(OrganizationMember).filter(OrganizationMember.organization_id == uuid.UUID(org)).delete()
        db.commit()
    finally:
        db.close()

    _send()

    (n,) = _rows(org)
    assert (n.status, n.last_error) == ("skipped", "no recipients") and mailbox.sent == []


def test_the_kill_switch_stops_queueing_and_sending(mailbox, monkeypatch):
    org, agent, number = _live_org()
    _analyzed(org, agent, number, LEAD)
    assert len(_rows(org)) == 1
    monkeypatch.setattr(get_settings(), "OWNER_NOTIFICATIONS_ENABLED", False)

    assert _send() == {"sent": 0, "skipped": 0, "failed": 0, "retry": 0}
    org2, agent2, number2 = _live_org()
    _analyzed(org2, agent2, number2, LEAD)
    assert _rows(org2) == [] and mailbox.sent == []


def test_the_worker_pass_sends_owner_email_even_when_retell_is_not_configured(mailbox, monkeypatch):
    from app.workers import run_worker

    org, agent, number = _live_org()
    _analyzed(org, agent, number, LEAD)

    def no_retell():
        raise RuntimeError("Retell is not configured")

    monkeypatch.setattr(run_worker, "RetellPhoneProvider", no_retell)
    summary = run_worker.run_once()

    assert "provider_jobs" in summary["failed_jobs"]  # the provider jobs were skipped ...
    assert [m[1] for m in _mail_for(org)] == ["New lead: Dana Plumber"]  # ... the owner was still told
    assert _rows(org)[0].status == "sent"


def test_the_worker_pass_runs_the_notification_jobs_after_a_provider_job_crashes(mailbox):
    from app.workers import run_worker

    class Exploding(FakePhone):
        def list_numbers(self, *a, **k):
            raise RuntimeError("provider down")

        def unassign_agent(self, *a, **k):
            raise RuntimeError("provider down")

    org, agent, number = _live_org()
    _analyzed(org, agent, number, LEAD)

    summary = run_worker.run_once(phone_provider=Exploding())

    assert "owner_notifications" in summary and _rows(org)[0].status == "sent"


def test_deleting_an_organization_removes_its_notifications():
    from sqlalchemy import delete, text

    from app.db.models.tenancy import Organization

    org, agent, number = _live_org()
    _analyzed(org, agent, number, LEAD)
    assert len(_rows(org)) == 1
    db = SessionLocal()
    try:
        db.execute(delete(Organization).where(Organization.id == uuid.UUID(org)))  # what account deletion does
        db.commit()
        assert db.execute(text("select count(*) from owner_notifications where organization_id = :o"), {"o": org}).scalar() == 0
    finally:
        db.close()


# =========================================================== receptionist paused

def test_pausing_for_exhausted_credits_notifies_once_and_says_the_number_is_kept(mailbox, monkeypatch):
    org, provider = _paused_setup(monkeypatch)

    assert _enforce(org, provider)["suspended"] == 1
    (n,) = _rows(org, "service_paused")
    assert n.payload == {"reason": "credits_exhausted"}

    _enforce(org, provider)  # a later pass finds nothing live: no second notification
    assert len(_rows(org, "service_paused")) == 1

    _send()
    to, subject, body = next(m for m in _mail_for(org) if m[1] == "Your Atla receptionist has been paused")
    assert "minutes have run out" in body and "number is kept" in body
    assert f"{FRONTEND}/dashboard/settings/billing" in body and f"{FRONTEND}/dashboard/go-live" in body


def test_pausing_again_after_the_customer_resumes_notifies_again(mailbox, monkeypatch):
    org, provider = _paused_setup(monkeypatch)
    _enforce(org, provider)
    db = SessionLocal()
    try:
        for agent in db.query(Agent).filter(Agent.organization_id == uuid.UUID(org)):
            agent.status = AgentStatus.active  # the customer pressed Go live again
        db.commit()
    finally:
        db.close()

    _enforce(org, provider, now=datetime.now(timezone.utc) + timedelta(seconds=1))

    assert len(_rows(org, "service_paused")) == 2


@pytest.mark.parametrize(
    "status, age_days, reason",
    [("canceled", 1, "subscription_lapsed"), ("paused", 1, "subscription_lapsed"), ("past_due", 10, "payment_past_due")],
)
def test_pausing_for_a_lapsed_subscription_gives_the_right_reason(mailbox, status, age_days, reason):
    org, _agent, number = _live_org()
    subscribe(org, status=status, start=datetime.now(timezone.utc) - timedelta(days=age_days))
    provider = FakePhone()
    provider.numbers[number] = {"nickname": "n", "agent": "agent_x"}

    _enforce(org, provider)

    (n,) = _rows(org, "service_paused")
    assert n.payload == {"reason": reason}
    _send()
    body = _mail_for(org)[0][2]
    assert f"{get_settings().NUMBER_RELEASE_DELAY_DAYS} days" in body and f"{FRONTEND}/dashboard/go-live" in body


def test_nothing_is_announced_when_nothing_was_live():
    org, _agent, number = _live_org()
    db = SessionLocal()
    try:
        db.query(Agent).filter(Agent.organization_id == uuid.UUID(org)).update({"status": AgentStatus.paused})
        from app.db.models.telephony import PhoneNumber

        db.query(PhoneNumber).filter(PhoneNumber.organization_id == uuid.UUID(org)).update({"agent_id": None})
        db.commit()
    finally:
        db.close()
    subscribe(org, status="canceled", start=datetime.now(timezone.utc) - timedelta(days=1))
    provider = FakePhone()  # agent paused and the number has no agent attached: there is nothing to switch off

    _enforce(org, provider)

    assert _rows(org) == []


def test_a_provider_failure_while_pausing_queues_nothing_until_the_pause_really_happens(monkeypatch):
    org, provider = _paused_setup(monkeypatch)
    provider.fail_unassign = 1

    result = _enforce(org, provider)
    assert result["errors"] == 1 and _rows(org) == []

    _enforce(org, provider)
    assert len(_rows(org, "service_paused")) == 1


# =========================================================== low / empty credits

def _credit_org(monkeypatch, used_minutes, included=100, live=True, subscribed=True):
    monkeypatch.setitem(PLANS, "starter", Plan("starter", "Starter", 1, PLANS["starter"].features, included))
    org, _agent, _number = _live_org()
    if subscribed:
        subscribe(org)
    if used_minutes:
        add_usage(org, used_minutes * MIN)
    if not live:
        db = SessionLocal()
        try:
            db.query(Agent).filter(Agent.organization_id == uuid.UUID(org)).update({"status": AgentStatus.paused})
            from app.db.models.telephony import PhoneNumber, PhoneNumberStatus

            db.query(PhoneNumber).filter(PhoneNumber.organization_id == uuid.UUID(org)).update({"status": PhoneNumberStatus.released})
            db.commit()
        finally:
            db.close()
    return org


def _credit_pass(*orgs):
    db = SessionLocal()
    try:
        return on.notify_credit_levels(db, organization_ids={uuid.UUID(o) for o in orgs})
    finally:
        db.close()


def test_low_credits_warn_once_per_billing_period(mailbox, monkeypatch):
    org = _credit_org(monkeypatch, used_minutes=85)  # 15 of 100 minutes left, under the 20% warning line

    _credit_pass(org)
    _credit_pass(org)
    _credit_pass(org)

    (n,) = _rows(org)
    assert n.kind == "low_credits"
    _send()
    to, subject, body = _mail_for(org)[0]
    assert subject == "Your Atla call minutes are running low" and "about 15 minutes" in body
    assert f"{FRONTEND}/dashboard/settings/billing" in body


def test_empty_credits_get_their_own_warning_that_explains_the_grace(mailbox, monkeypatch):
    org = _credit_org(monkeypatch, used_minutes=100)

    _credit_pass(org)

    (n,) = _rows(org)
    assert n.kind == "credits_empty"
    _send()
    body = _mail_for(org)[0][2]
    assert "used all your call minutes" in body and f"{get_settings().OVERAGE_GRACE_MINUTES} minutes over" in body


def test_a_healthy_balance_unsubscribed_or_paused_organization_gets_no_credit_warning(monkeypatch):
    healthy = _credit_org(monkeypatch, used_minutes=10)
    unsubscribed = _credit_org(monkeypatch, used_minutes=100, subscribed=False)
    paused = _credit_org(monkeypatch, used_minutes=100, live=False)

    _credit_pass(healthy, unsubscribed, paused)

    assert _rows(healthy) == [] and _rows(unsubscribed) == [] and _rows(paused) == []


def test_a_credit_warning_is_dropped_if_the_customer_added_minutes_before_it_was_sent(mailbox, monkeypatch):
    org = _credit_org(monkeypatch, used_minutes=85)
    _credit_pass(org)
    monkeypatch.setattr(on, "credit_summary", lambda db, org_id, now=None: SimpleNamespace(status="ok", available_seconds=99999))

    _send()

    assert _mail_for(org) == [] and _rows(org)[0].status == "skipped"


def test_an_empty_warning_is_dropped_if_the_receptionist_was_already_paused(mailbox, monkeypatch):
    org = _credit_org(monkeypatch, used_minutes=100)
    _credit_pass(org)
    db = SessionLocal()
    try:
        db.query(Agent).filter(Agent.organization_id == uuid.UUID(org)).update({"status": AgentStatus.paused})
        from app.db.models.telephony import PhoneNumber, PhoneNumberStatus

        db.query(PhoneNumber).filter(PhoneNumber.organization_id == uuid.UUID(org)).update({"status": PhoneNumberStatus.released})
        db.commit()
    finally:
        db.close()

    _send()

    assert _mail_for(org) == [] and _rows(org)[0].status == "skipped"


def test_the_worker_checks_credit_levels_on_an_interval_not_every_pass(monkeypatch):
    calls = []
    monkeypatch.setattr(on, "notify_credit_levels", lambda db, now=None: calls.append(1) or {"checked": 0, "queued": 0, "errors": 0})
    monkeypatch.setattr(get_settings(), "CREDIT_ALERT_INTERVAL_SECONDS", 3600)
    monkeypatch.setattr(on, "_last_credit_pass", None)

    db = SessionLocal()
    try:
        assert on.credit_alert_pass(db) is not None
        assert on.credit_alert_pass(db) is None and on.credit_alert_pass(db) is None
    finally:
        db.close()
    assert len(calls) == 1


# =========================================================== failed payment

def _snapshot(org, sub_id, status, period=("2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z")):
    return {
        "id": sub_id, "status": status, "custom_data": {"organization_id": org},
        "current_billing_period": {"starts_at": period[0], "ends_at": period[1]},
        "items": [{"status": "active", "quantity": 1, "price": {"id": "pri_starter"}}],
    }


def _apply(org, sub_id, status, at):
    db = SessionLocal()
    try:
        applied = sync_subscription(db, _snapshot(org, sub_id, status), at)
        db.commit()
        return applied
    finally:
        db.close()


def test_a_transition_into_past_due_queues_one_payment_failed_notification(monkeypatch):
    monkeypatch.setattr(get_settings(), "PADDLE_STARTER_PRICE_ID", "pri_starter")
    _c, org = new_org("pay")
    sub = f"sub_{uuid.uuid4().hex[:8]}"
    t = datetime.now(timezone.utc) - timedelta(hours=3)

    _apply(org, sub, "active", t)
    assert _rows(org) == []
    _apply(org, sub, "past_due", t + timedelta(hours=1))
    _apply(org, sub, "past_due", t + timedelta(hours=1))  # same snapshot again (reconciler / redelivery)
    _apply(org, sub, "past_due", t + timedelta(hours=2))  # still past due: not a new failure

    (n,) = _rows(org)
    assert n.kind == "payment_failed" and n.payload == {"subscription_id": sub}


def test_each_new_payment_failure_after_recovery_is_announced(monkeypatch):
    monkeypatch.setattr(get_settings(), "PADDLE_STARTER_PRICE_ID", "pri_starter")
    _c, org = new_org("pay2")
    sub = f"sub_{uuid.uuid4().hex[:8]}"
    t = datetime.now(timezone.utc) - timedelta(hours=5)
    for i, status in enumerate(["active", "past_due", "active", "past_due"]):
        _apply(org, sub, status, t + timedelta(hours=i))

    assert len(_rows(org, "payment_failed")) == 2


def test_a_subscription_first_seen_as_past_due_is_announced(monkeypatch):
    monkeypatch.setattr(get_settings(), "PADDLE_STARTER_PRICE_ID", "pri_starter")
    _c, org = new_org("pay3")

    _apply(org, f"sub_{uuid.uuid4().hex[:8]}", "past_due", datetime.now(timezone.utc))

    assert len(_rows(org, "payment_failed")) == 1


def test_the_paddle_webhook_queues_the_notification_in_the_same_transaction(monkeypatch):
    from tests.test_billing import SECRET, _event as paddle_event, _post as paddle_post

    s = get_settings()
    monkeypatch.setattr(s, "PADDLE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(s, "PADDLE_STARTER_PRICE_ID", "pri_starter")
    _c, org = new_org("pay4")
    sub = f"sub_{uuid.uuid4().hex[:8]}"
    ev = paddle_event("subscription.past_due", sub, org, status="past_due", at="2026-10-02T00:00:00.000000Z")

    assert paddle_post(ev).status_code == 204
    assert paddle_post(ev).status_code == 204  # duplicate delivery

    (n,) = _rows(org)
    assert n.kind == "payment_failed" and n.payload["subscription_id"] == sub


def test_payment_failed_email_states_when_service_stops_and_links_to_billing(mailbox, monkeypatch):
    monkeypatch.setattr(get_settings(), "PADDLE_STARTER_PRICE_ID", "pri_starter")
    monkeypatch.setattr(get_settings(), "PAST_DUE_GRACE_DAYS", 3)
    _c, org = new_org("pay5")
    sub = f"sub_{uuid.uuid4().hex[:8]}"
    failed_at = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    _apply(org, sub, "past_due", failed_at)

    _send()

    to, subject, body = _mail_for(org)[0]
    assert subject == "Action needed: your Atla payment failed"
    assert "Sun Nov 8, 2026 12:00 PM UTC" in body  # failure time + PAST_DUE_GRACE_DAYS, the same cut-off entitlement.py applies
    assert f"{FRONTEND}/dashboard/settings/billing" in body


def test_payment_failed_email_is_dropped_if_the_customer_already_paid(mailbox, monkeypatch):
    monkeypatch.setattr(get_settings(), "PADDLE_STARTER_PRICE_ID", "pri_starter")
    _c, org = new_org("pay6")
    sub = f"sub_{uuid.uuid4().hex[:8]}"
    t = datetime.now(timezone.utc) - timedelta(hours=2)
    _apply(org, sub, "past_due", t)
    _apply(org, sub, "active", t + timedelta(hours=1))

    _send()

    assert _mail_for(org) == [] and _rows(org)[0].status == "skipped"
    db = SessionLocal()
    try:
        assert db.query(Subscription).filter_by(external_subscription_id=sub).one().status == "active"
    finally:
        db.close()
