"""Call, receptionist and phone in-app notifications (Task 6): created in the same transaction as the event, once per real event,
tenant scoped, text from fixed templates only, and never emailed."""
import uuid

import pytest

from app.core import email as email_mod
from app.db.base import SessionLocal
from app.db.models.calls import Call, CallDirection
from app.db.models.telephony import PhoneNumber, PhoneNumberStatus
from app.db.models.tenancy import OrgRole
from app.db.models.voice_agent import Agent, AgentStatus
from app.services import notifications as svc
from app.services import owner_notifications as on
from app.services.activation import activate_receptionist
from app.services.phone_provisioning import active_number, provision_number, release_number
from tests.test_auto_provisioning import PayFake, fake  # noqa: F401  (fixture: fake phone provider wired in everywhere)
from tests.test_call_pipeline import _call, _key, _post, _seed  # noqa: F401  (autouse: the webhook signing key)
from tests.test_call_pipeline import _new_org as _pipe_org
from tests.test_notifications import TestClientFor, _add_user, _owner_id, _rows, _url  # noqa: F401


def _explode(*_a, **_k):
    raise AssertionError("an email was attempted for an activity notification")


@pytest.fixture(autouse=True)
def _no_email_ever(monkeypatch):
    monkeypatch.setattr(on, "send_email_result", _explode)
    monkeypatch.setattr(email_mod, "send_email_result", _explode)
    monkeypatch.setattr(email_mod, "send_email", _explode)


def _end(org_agent_number, reason, caller="+15559991234", cid=None, event="call_ended"):
    agent, number = org_agent_number
    cid = cid or f"call_{uuid.uuid4().hex[:10]}"
    assert _post(event, _call(cid, agent, number, disconnection_reason=reason, from_number=caller)).status_code == 204
    return cid


def _kinds(org, user_id=None):
    return [r.kind for r in _rows(org, user_id)]


# ------------------------------------------------------------------ calls
# Retell's documented DisconnectionReason enum (call object schema + the "Debug call disconnection" page): 39 values, each decided once.
OFFICIAL_MISSED = [
    "concurrency_limit_reached", "no_concurrency_fallback", "no_valid_payment", "credit_exhausted", "budget_reached", "scam_detected",
    "error_llm_websocket_open", "error_llm_websocket_lost_connection", "error_llm_websocket_runtime",
    "error_llm_websocket_corrupt_payload", "error_no_audio_received", "error_asr", "error_retell", "error_unknown",
    "registered_call_timeout",
]
OFFICIAL_SERVED = ["user_hangup", "agent_hangup", "user_requested_dnc", "user_requested_callback", "voicemail_reached", "ivr_reached",
                   "inactivity", "max_duration_reached", "call_take_over", "manual_stopped"]
OFFICIAL_TRANSFER_LEGS = ["transfer_bridged", "transfer_cancelled"]  # the transfer agent's own call in a warm transfer
OFFICIAL_OUTBOUND_ONLY = ["dial_busy", "dial_failed", "dial_no_answer", "invalid_destination", "user_declined", "marked_as_spam",
                          "network_blocked", "telephony_provider_permission_denied", "telephony_provider_unavailable", "sip_routing_error"]
OFFICIAL_WEB_ONLY = ["error_user_not_joined"]
OFFICIAL_TRANSFERRED = ["call_transfer"]
OFFICIAL_ALL = (OFFICIAL_MISSED + OFFICIAL_SERVED + OFFICIAL_TRANSFER_LEGS + OFFICIAL_OUTBOUND_ONLY + OFFICIAL_WEB_ONLY + OFFICIAL_TRANSFERRED)


def test_every_documented_disconnection_reason_is_classified_exactly_once():
    from app.services.call_ingestion import MISSED_REASONS, NOT_MISSED_REASONS

    assert len(OFFICIAL_ALL) == 39 == len(set(OFFICIAL_ALL))
    assert set(MISSED_REASONS) == set(OFFICIAL_MISSED)
    assert set(NOT_MISSED_REASONS) == set(OFFICIAL_ALL) - set(OFFICIAL_MISSED)
    assert not (set(MISSED_REASONS) & set(NOT_MISSED_REASONS))


@pytest.mark.parametrize("reason", OFFICIAL_MISSED)
def test_an_inbound_call_that_failed_to_connect_is_a_missed_call_once(reason):
    _c, org = _pipe_org()
    seeded = _seed(org)
    cid = _end(seeded, reason)
    _end(seeded, reason, cid=cid, event="call_analyzed")  # the analysis event for the same call
    _end(seeded, reason, cid=cid)  # a redelivery
    rows = _rows(org)
    assert [(r.kind, r.category, r.severity, r.link) for r in rows] == [("missed_call", "calls", "warning", "/dashboard/calls")]
    assert rows[0].body == "A caller (number ending 1234) could not be connected to your receptionist."
    assert reason not in rows[0].title + rows[0].body and "+1555999" not in rows[0].body  # no provider text, no full number


@pytest.mark.parametrize("reason", OFFICIAL_SERVED + OFFICIAL_TRANSFER_LEGS + OFFICIAL_OUTBOUND_ONLY + OFFICIAL_WEB_ONLY + [None])
def test_a_call_that_was_served_or_cannot_be_an_inbound_failure_is_not_a_missed_call(reason):
    _c, org = _pipe_org()
    _end(_seed(org), reason)
    assert _kinds(org) == []


@pytest.mark.parametrize("reason", ["error_future_thing_retell_adds", "error_"])
def test_an_unknown_future_error_reason_counts_as_missed_but_an_unknown_other_reason_does_not(reason):
    _c, org = _pipe_org()
    _end(_seed(org), reason)
    assert _kinds(org) == ["missed_call"]
    _c, org = _pipe_org()
    _end(_seed(org), "some_future_normal_ending")
    assert _kinds(org) == []


def test_a_transferred_call_produces_exactly_one_call_transferred_per_recipient():
    from app.db.models.notifications import Notification

    _c, org = _pipe_org()
    _a, admin = _add_user(org, OrgRole.admin)
    _m, member = _add_user(org, OrgRole.member)
    seeded = _seed(org)
    cid = _end(seeded, "call_transfer")
    for event in ("call_ended", "call_analyzed", "call_ended", "call_analyzed"):  # the whole event stream, twice over
        _end(seeded, "call_transfer", cid=cid, event=event)
    rows = _rows(org)
    assert [(r.kind, r.severity) for r in rows if r.recipient_user_id == _owner_id(org)] == [("call_transferred", "info")]
    assert "ending 1234" in rows[0].body and "missed_call" not in [r.kind for r in rows]
    db = SessionLocal()
    try:
        per_user = {u: db.query(Notification).filter_by(organization_id=uuid.UUID(org), recipient_user_id=u, kind="call_transferred").count()
                    for u in (_owner_id(org), admin, member)}
    finally:
        db.close()
    assert per_user == {_owner_id(org): 1, admin: 1, member: 1}


def test_a_transfer_whose_final_event_arrives_with_a_different_reason_does_not_flip_the_outcome_into_a_second_notification():
    _c, org = _pipe_org()
    seeded = _seed(org)
    cid = _end(seeded, "call_transfer")
    _end(seeded, "user_hangup", cid=cid, event="call_analyzed")  # a later event with a normal reason
    assert _kinds(org) == ["call_transferred"]


def test_a_test_call_is_not_customer_activity():
    from app.services.call_ingestion import _notify_call_outcome

    _c, org = _pipe_org()
    _seed(org)
    db = SessionLocal()
    try:
        agent = db.query(Agent).filter_by(organization_id=uuid.UUID(org)).one()
        call = Call(organization_id=uuid.UUID(org), agent_id=agent.id, retell_call_id=f"call_{uuid.uuid4().hex[:8]}",
                    direction=CallDirection.test, status="ended", disconnect_reason="error_asr")
        db.add(call)
        db.flush()
        _notify_call_outcome(db, call)
        db.commit()
    finally:
        db.close()
    assert _kinds(org) == []


def test_missed_calls_reach_owner_admin_and_member_but_only_their_own_organization():
    _c, org_a = _pipe_org()
    _c, org_b = _pipe_org()
    _admin_client, admin = _add_user(org_a, OrgRole.admin)
    _member_client, member = _add_user(org_a, OrgRole.member)
    _end(_seed(org_a), "error_asr")
    assert all("missed_call" in _kinds(org_a, u) for u in (_owner_id(org_a), admin, member))
    assert _kinds(org_b) == []
    assert TestClientFor(org_b).get(_url(org_b)).json()["items"] == []
    assert [i["kind"] for i in TestClientFor(org_a).get(_url(org_a), params={"category": "calls"}).json()["items"]] == ["missed_call"]


def test_a_notification_failure_never_blocks_call_ingestion(monkeypatch):
    monkeypatch.setattr(svc, "_compose", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("compose broke")))
    _c, org = _pipe_org()
    cid = _end(_seed(org), "error_asr")
    db = SessionLocal()
    try:
        assert db.query(Call).filter_by(retell_call_id=cid).one().status == "ended"
    finally:
        db.close()
    assert _kinds(org) == []


# ------------------------------------------------------------------ phone numbers
def _provision(org, f):
    db = SessionLocal()
    try:
        return provision_number(db, uuid.UUID(org), "US", "415", f)
    finally:
        db.close()


def test_a_new_number_notifies_once_and_asking_again_notifies_nobody(fake):
    _c, org = _pipe_org()
    _provision(org, fake)
    _provision(org, fake)
    _provision(org, fake)
    rows = _rows(org)
    assert [(r.kind, r.category, r.link) for r in rows] == [("phone_number_ready", "phone", "/dashboard/go-live")]


def test_a_refused_purchase_says_failed_and_a_payment_problem_says_so_and_each_is_critical(fake):
    _c, org = _pipe_org()
    fake.reject_purchases = 1
    with pytest.raises(Exception):  # noqa: B017  ProvisioningFailed
        _provision(org, fake)
    _c2, org2 = _pipe_org()
    fake.payment_required = 1
    with pytest.raises(Exception):  # noqa: B017  ProvisioningPaymentRequired
        _provision(org2, fake)
    one, two = _rows(org), _rows(org2)
    assert [(r.kind, r.severity) for r in one] == [("phone_provisioning_failed", "critical")]
    assert "couldn't set up" in one[0].body and "payment problem" not in one[0].body
    assert [(r.kind, r.severity) for r in two] == [("phone_provisioning_failed", "critical")] and "payment problem on our side" in two[0].body
    assert "402" not in one[0].body + two[0].body and "Retell" not in one[0].body + two[0].body


def test_an_ambiguous_purchase_that_is_still_being_settled_notifies_nobody(fake):
    _c, org = _pipe_org()
    fake.timeout_without_purchase = 1
    with pytest.raises(Exception):  # noqa: B017  ProvisioningPending
        _provision(org, fake)
    assert _kinds(org) == []


def _release(org, f, reason):
    db = SessionLocal()
    try:
        release_number(db, active_number(db, uuid.UUID(org)), f, reason=reason)
    finally:
        db.close()


def test_releasing_a_number_notifies_once_with_the_reason_and_a_retry_adds_nothing(fake):
    _c, org = _pipe_org()
    _provision(org, fake)
    db = SessionLocal()
    try:
        phone = active_number(db, uuid.UUID(org))
        pid = phone.id
        release_number(db, phone, fake, reason="entitlement")
        release_number(db, db.get(PhoneNumber, pid), fake, reason="entitlement")  # a retry of the same release
        assert db.get(PhoneNumber, pid).status == PhoneNumberStatus.released
    finally:
        db.close()
    released = [r for r in _rows(org) if r.kind == "phone_number_released"]
    assert len(released) == 1 and released[0].severity == "warning" and "subscription ended" in released[0].body


def test_a_customer_release_says_as_requested_and_account_deletion_tells_nobody(fake):
    _c, org = _pipe_org()
    _provision(org, fake)
    _release(org, fake, "customer")
    assert "as requested" in next(r.body for r in _rows(org) if r.kind == "phone_number_released")
    _c, org2 = _pipe_org()
    _provision(org2, fake)
    _release(org2, fake, "account_deleted")
    assert "phone_number_released" not in _kinds(org2)


# ------------------------------------------------------------------ receptionist
def _go_live(org, f):
    """A synced agent and a live number registered with the fake provider, then activation."""
    agent_rid, number = _seed(org)
    f.numbers[number] = {"nickname": "n", "agent": None}
    db = SessionLocal()
    try:
        agent = db.query(Agent).filter_by(organization_id=uuid.UUID(org)).one()
        agent.status = AgentStatus.draft
        phone = db.query(PhoneNumber).filter_by(organization_id=uuid.UUID(org)).one()
        db.commit()
        return agent.id, phone.id
    finally:
        db.close()


def _activate(org, ids, f):
    db = SessionLocal()
    try:
        return activate_receptionist(db, uuid.UUID(org), ids[0], ids[1], f)
    finally:
        db.close()


def test_going_live_notifies_once_and_repeating_it_notifies_nobody_but_going_live_again_after_a_pause_does(fake):
    _c, org = _pipe_org()
    ids = _go_live(org, fake)
    assert _activate(org, ids, fake).status == AgentStatus.active
    assert _activate(org, ids, fake).status == AgentStatus.active  # already live: nothing new to say
    assert _kinds(org) == ["receptionist_live"]
    assert (_rows(org)[0].category, _rows(org)[0].link) == ("receptionist", "/dashboard/go-live")

    db = SessionLocal()
    try:
        db.get(Agent, ids[0]).status = AgentStatus.paused
        db.commit()
    finally:
        db.close()
    _activate(org, ids, fake)
    assert _kinds(org).count("receptionist_live") == 2


def test_a_refused_activation_notifies_nobody(fake):
    _c, org = _pipe_org()
    ids = _go_live(org, fake)
    del fake.numbers[next(iter(fake.numbers))]  # the provider no longer knows the number
    with pytest.raises(Exception):  # noqa: B017  ActivationBlocked
        _activate(org, ids, fake)
    assert _kinds(org) == []


def test_only_owners_and_admins_hear_about_receptionist_and_phone_events_and_members_do_not(fake):
    _c, org = _pipe_org()
    _admin_client, admin = _add_user(org, OrgRole.admin)
    _member_client, member = _add_user(org, OrgRole.member)
    _activate(org, _go_live(org, fake), fake)
    assert "receptionist_live" in _kinds(org, admin) and "receptionist_live" in _kinds(org, _owner_id(org))
    assert _kinds(org, member) == []


# ------------------------------------------------------------------ safety
def test_no_activity_kind_is_email_eligible_and_all_are_in_the_catalogue():
    new = {"missed_call", "call_transferred", "receptionist_live", "phone_number_ready", "phone_provisioning_failed", "phone_number_released"}
    assert new <= set(svc.CATALOGUE) and not (new & on.EMAIL_KINDS) and on.EMAIL_KINDS == frozenset()
    for kind in new:
        category, _sev, link, _aud = svc.CATALOGUE[kind]
        assert category in ("calls", "receptionist", "phone") and link in ("/dashboard/calls", "/dashboard/go-live")
        assert svc.safe_link(link) == link


def test_message_text_is_a_fixed_template_never_payload_text():
    _c, org = _pipe_org()
    db = SessionLocal()
    try:
        svc.create(db, uuid.UUID(org), "missed_call", "t1", {"caller_last4": "<script>9876</script>", "reason": "error_asr"})
        svc.create(db, uuid.UUID(org), "missed_call", "t2", {"caller_last4": "12"})
        svc.create(db, uuid.UUID(org), "phone_provisioning_failed", "t3", {"reason": "Bearer sk_live_123"})
        svc.create(db, uuid.UUID(org), "phone_number_released", "t4", {"reason": "x\nBcc: a@b.c"})
        db.commit()
    finally:
        db.close()
    body = {r.dedupe_key: r.body for r in _rows(org)}
    assert body["t1"] == "A caller (number ending 9876) could not be connected to your receptionist."
    assert body["t2"] == "A caller could not be connected to your receptionist."
    assert "sk_live" not in body["t3"] and "Bcc" not in body["t4"] and "as requested" in body["t4"]
