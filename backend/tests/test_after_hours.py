"""
Level 8C: after-hours behaviour.

  * hours_schedule        one evaluator: weekly windows, date exceptions, DST, never guesses (unknown == not open)
  * inbound routing       one policy, two adapters (RETELL webhook, SIMULATED sandbox route): identical decisions
  * fail-closed           only a positively confirmed "open" ever returns the normal agent
  * closed twin           no transfer tool; created/updated/activated only for businesses that opted in
  * lead + notification   received_after_hours from the CALL's start time; the owner's email says so
  * opt-in                a business that never enabled a schedule behaves exactly as before

Real Postgres, real routes and signature verification. Nothing here talks to Retell: the provider is a recorder.
"""
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.auth.deps import get_current_user
from app.config import Settings, get_settings
from app.db.base import SessionLocal
from app.db.models.business import Business
from app.db.models.crm import Lead
from app.db.models.telephony import PhoneNumber
from app.db.models.tenancy import User
from app.db.models.voice_agent import Agent
from app.main import app
from app.services import hours_schedule as hs
from app.services import inbound_routing
from app.services.activation import ActivationBlocked, activate_receptionist, verify_receptionist_live
from app.webhooks import retell_inbound
from tests.billing_helpers import new_org
from tests.fakes import FakePhone
from tests.test_inbound_call_resolution import KEY, _cid, _event, _post, _retell_key  # noqa: F401  (autouse)
from tests.test_call_pipeline import _seed
from tests.test_owner_notifications import LEAD, _mail_for, _send, mailbox  # noqa: F401  (autouse)

settings = get_settings()
NORMAL, CLOSED = "agent_normal_x", "agent_closed_x"


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


WEEKDAY = {"enabled": True, "weekly": {d: [["09:00", "12:00"], ["13:00", "17:00"]] for d in ("mon", "tue", "wed", "thu", "fri")}}


# ======================================================== evaluator

# 2026-11-10 is a Tuesday. America/Chicago is UTC-6 (CST) then.
@pytest.mark.parametrize("hour,minute,expected", [
    (8, 59, "closed"), (9, 0, "open"), (11, 59, "open"), (12, 0, "closed"), (12, 30, "closed"),
    (13, 0, "open"), (16, 59, "open"), (17, 0, "closed"), (23, 59, "closed"),
])
def test_multiple_windows_use_start_inclusive_end_exclusive(hour, minute, expected):
    at = utc(2026, 11, 10, 0, 0) + timedelta(hours=hour + 6, minutes=minute)
    assert hs.evaluate(WEEKDAY, "America/Chicago", at) == expected


def test_a_day_with_no_windows_is_closed():
    assert hs.evaluate(WEEKDAY, "America/Chicago", utc(2026, 11, 14, 18, 0)) == "closed"  # Saturday noon


def test_the_local_date_not_the_utc_date_decides_the_weekday():
    # 2026-11-10 23:00 CST is already Wednesday in UTC; the business is still on Tuesday evening.
    assert hs.evaluate({"enabled": True, "weekly": {"tue": [["20:00", "24:00"]]}}, "America/Chicago", utc(2026, 11, 11, 5, 0)) == "open"


def test_date_exceptions_replace_the_weekday_and_can_close_or_shorten_a_day():
    sched = {**WEEKDAY, "exceptions": {"2026-11-10": [], "2026-11-11": [["10:00", "11:00"]]}}
    assert hs.evaluate(sched, "America/Chicago", utc(2026, 11, 10, 16, 0)) == "closed"  # Tuesday, closed by exception
    assert hs.evaluate(sched, "America/Chicago", utc(2026, 11, 11, 16, 30)) == "open"  # special hours
    assert hs.evaluate(sched, "America/Chicago", utc(2026, 11, 11, 15, 0)) == "closed"  # normal 9:00 no longer applies


def test_daylight_saving_is_handled_by_the_timezone_database():
    sched = {"enabled": True, "weekly": {"sun": [["08:00", "12:00"]]}}
    # US spring-forward is Sun 2026-03-08 02:00; after it Chicago is UTC-5 (CDT).
    assert hs.evaluate(sched, "America/Chicago", utc(2026, 3, 8, 14, 0)) == "open"   # 09:00 CDT
    assert hs.evaluate(sched, "America/Chicago", utc(2026, 3, 8, 13, 0)) == "open"   # 08:00 CDT
    assert hs.evaluate(sched, "America/Chicago", utc(2026, 3, 8, 12, 59)) == "closed"  # 07:59 CDT
    assert hs.evaluate(sched, "America/Chicago", utc(2026, 3, 8, 17, 0)) == "closed"  # 12:00 CDT


@pytest.mark.parametrize("schedule,tz,instant", [
    (None, "America/Chicago", utc(2026, 11, 10, 16)),
    ({"enabled": False, "weekly": WEEKDAY["weekly"]}, "America/Chicago", utc(2026, 11, 10, 16)),
    (WEEKDAY, None, utc(2026, 11, 10, 16)),
    (WEEKDAY, "Mars/Base", utc(2026, 11, 10, 16)),
    (WEEKDAY, "CST", utc(2026, 11, 10, 16)),
    (WEEKDAY, "America/Chicago", None),
    ({"enabled": True, "weekly": {"tue": "09:00-17:00"}}, "America/Chicago", utc(2026, 11, 10, 16)),
    ({"enabled": True, "weekly": {"tue": [["17:00", "09:00"]]}}, "America/Chicago", utc(2026, 11, 10, 16)),
    ({"enabled": True, "weekly": {"tue": [["09:00", "25:00"]]}}, "America/Chicago", utc(2026, 11, 10, 16)),
    ({"enabled": True, "weekly": "always"}, "America/Chicago", utc(2026, 11, 10, 16)),
    ({"enabled": "yes", "weekly": WEEKDAY["weekly"]}, "America/Chicago", utc(2026, 11, 10, 16)),
    ("open 24/7", "America/Chicago", utc(2026, 11, 10, 16)),
    ({"enabled": True}, "America/Chicago", utc(2026, 11, 10, 16)),
])
def test_anything_it_cannot_evaluate_is_unknown_never_open(schedule, tz, instant):
    assert hs.evaluate(schedule, tz, instant) == "unknown"
    assert hs.received_after_hours(schedule, tz, instant) is None


@pytest.mark.parametrize("bad", [
    {"enabled": True, "weekly": {"funday": [["09:00", "10:00"]]}},
    {"enabled": True, "weekly": {"mon": [["09:00", "10:00"], ["09:30", "11:00"]]}},   # overlap
    {"enabled": True, "weekly": {"mon": [["10:00", "10:00"]]}},                        # empty window
    {"enabled": True, "weekly": {"mon": [["22:00", "02:00"]]}},                        # overnight must be split
    {"enabled": True, "weekly": {"mon": [["09:00", "24:30"]]}},
    {"enabled": True, "weekly": {"mon": [["9:00", "10:00"]]}},
    {"enabled": True, "weekly": {"mon": [["0%d:00" % i, "0%d:30" % i] for i in range(1, 6)]}},  # 5 windows
    {"enabled": True, "weekly": {"mon": [["09:00", "10:00"]]}, "exceptions": {"2026-02-30": []}},
    {"enabled": True, "weekly": {"mon": [["09:00", "10:00"]]}, "exceptions": {"Christmas": []}},
    {"enabled": True, "weekly": {"mon": [["09:00", "10:00"]]}, "holidays": "us"},      # no holiday library
    {"enabled": True, "weekly": {}},                                                   # enabled, no hours at all
])
def test_validation_rejects_malformed_schedules(bad):
    with pytest.raises(ValueError):
        hs.validate_schedule(bad)


def test_validation_normalises_and_allows_midnight_end_and_split_overnight():
    out = hs.validate_schedule({"enabled": True, "weekly": {"fri": [["13:00", "17:00"], ["09:00", "12:00"], ["22:00", "24:00"]], "sat": [["00:00", "02:00"]]}})
    assert out["weekly"]["fri"] == [["09:00", "12:00"], ["13:00", "17:00"], ["22:00", "24:00"]]
    assert hs.validate_schedule(None) is None


def test_the_lead_flag_follows_open_and_closed():
    assert hs.received_after_hours(WEEKDAY, "America/Chicago", utc(2026, 11, 10, 16)) is False
    assert hs.received_after_hours(WEEKDAY, "America/Chicago", utc(2026, 11, 11, 3)) is True


# ======================================================== routing: one policy, two adapters

def _setup(schedule=WEEKDAY, tz="America/Chicago", twin=True):
    """A live org with a number; returns (org, number). The schedule/timezone/twin are the scenario."""
    _c, org = new_org("hours")
    retell_agent, number = _seed(org)
    db = SessionLocal()
    try:
        agent = db.query(Agent).filter(Agent.organization_id == uuid.UUID(org)).one()
        agent.retell_agent_id = f"{NORMAL}_{uuid.uuid4().hex[:6]}"
        agent.closed_retell_agent_id = f"{CLOSED}_{uuid.uuid4().hex[:6]}" if twin else None
        biz = db.get(Business, agent.business_id)
        biz.hours_schedule, biz.timezone = schedule, tz
        db.commit()
        return org, number, agent.retell_agent_id, agent.closed_retell_agent_id
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _flag_and_clock(monkeypatch):
    monkeypatch.setattr(settings, "AFTER_HOURS_ROUTING_ENABLED", True)
    monkeypatch.setattr(retell_inbound, "_now", lambda: utc(2026, 11, 10, 16, 0))  # Tue 10:00 CST: open


def _signed(payload, key=KEY, ts=None, tamper=False):
    body = json.dumps(payload).encode()
    ts = ts or int(time.time() * 1000)
    digest = hmac.new(key.encode(), body + str(ts).encode(), hashlib.sha256).hexdigest()
    return TestClient(app).post(
        "/webhooks/retell/inbound",
        content=body + (b" " if tamper else b""),
        headers={"content-type": "application/json", "x-retell-signature": f"v={ts},d={digest}"},
    )


def _retell(to, at=None):
    """RETELL adapter: a genuine-shaped, correctly signed call_inbound webhook."""
    payload = {"event": "call_inbound", "call_inbound": {"agent_id": "x", "from_number": "+15550001111", "to_number": to}}
    with pytest.MonkeyPatch.context() as mp:
        if at is not None:
            mp.setattr(retell_inbound, "_now", lambda: at)
        r = _signed(payload)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def sandbox(monkeypatch):
    monkeypatch.setattr(Settings, "unmetered_test_calls_active", property(lambda self: True))
    user = _fake_user()
    app.dependency_overrides[get_current_user] = lambda: user
    yield
    app.dependency_overrides.pop(get_current_user, None)


def _fake_user():
    return User(email="sim@example.test", password_hash="x")


def _simulated(to, at=None):
    """SIMULATED adapter: the sandbox route, fake caller, pretend clock."""
    body = {"to_number": to, "from_number": "+15550002222"}
    if at is not None:
        body["at"] = at.isoformat()
    r = TestClient(app).post("/api/v1/dev/simulate-inbound", json=body)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["adapter"] == "simulated" and j["request"]["call_inbound"]["to_number"] == to
    return j["response"]


OPEN_AT, CLOSED_AT = utc(2026, 11, 10, 16, 0), utc(2026, 11, 11, 3, 0)  # Tue 10:00 CST / Tue 21:00 CST

# (id, setup kwargs, instant, expected status, expected target)
SCENARIOS = [
    ("open", {}, OPEN_AT, "open", "normal"),
    ("closed-evening", {}, CLOSED_AT, "closed", "closed"),
    ("closed-lunch", {}, utc(2026, 11, 10, 18, 30), "closed", "closed"),
    ("closed-by-exception", {"schedule": {**WEEKDAY, "exceptions": {"2026-11-10": []}}}, OPEN_AT, "closed", "closed"),
    ("special-hours-open", {"schedule": {**WEEKDAY, "exceptions": {"2026-11-14": [["10:00", "12:00"]]}}}, utc(2026, 11, 14, 17, 0), "open", "normal"),
    ("no-timezone", {"tz": None}, OPEN_AT, "unknown", "closed"),
    ("invalid-timezone", {"tz": "Mars/Base"}, OPEN_AT, "unknown", "closed"),
    ("corrupt-schedule", {"schedule": {"enabled": True, "weekly": {"tue": "all day"}}}, OPEN_AT, "unknown", "closed"),
    ("open-but-twin-missing", {"twin": False}, OPEN_AT, "unknown", "closed"),
    ("closed-and-twin-missing", {"twin": False}, CLOSED_AT, "closed", "closed"),
    ("not-opted-in", {"schedule": None}, CLOSED_AT, "not_enabled", "default"),
    ("opted-out", {"schedule": {**WEEKDAY, "enabled": False}}, CLOSED_AT, "not_enabled", "default"),
]


def _run(adapter, number, at):
    return _retell(number, at) if adapter == "retell" else _simulated(number, at)


@pytest.mark.parametrize("sid,kw,at,status,target", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_both_adapters_give_the_same_answer_and_only_open_selects_the_normal_agent(sandbox, sid, kw, at, status, target):
    _org, number, normal, closed = _setup(**kw)
    r, s = _run("retell", number, at), _run("simulated", number, at)
    assert r == s, "switching the adapter must not change the decision"
    inbound = r["call_inbound"]
    assert inbound["metadata"]["atla_route"] == status
    override = inbound.get("override_agent_id")
    if target == "normal":
        assert override == normal
    elif target == "closed":
        assert override in (closed, None) and override != normal  # the twin, or no override (number default = twin)
    else:
        assert override is None  # opted-out businesses are not touched


def test_the_decision_is_made_by_one_function_for_both_adapters(sandbox, monkeypatch):
    seen = []
    real = inbound_routing.route_inbound_call
    monkeypatch.setattr(retell_inbound, "route_inbound_call", lambda db, **kw: seen.append(kw) or real(db, **kw))
    _org, number, *_ = _setup()
    _run("retell", number, OPEN_AT)
    _run("simulated", number, OPEN_AT)
    assert len(seen) == 2 and seen[0] == seen[1] == {"to_number": number, "at": OPEN_AT}


def test_a_routing_error_never_selects_the_normal_agent(sandbox, monkeypatch):
    _org, number, normal, _closed = _setup()
    monkeypatch.setattr(inbound_routing, "_decide", lambda *a: (_ for _ in ()).throw(RuntimeError("db down")))
    for adapter in ("retell", "simulated"):
        out = _run(adapter, number, OPEN_AT)["call_inbound"]
        assert out["metadata"]["atla_route"] == "error" and out.get("override_agent_id") != normal


def test_a_database_failure_inside_the_policy_fails_closed(monkeypatch):
    _org, number, normal, _closed = _setup()
    db = SessionLocal()
    try:
        monkeypatch.setattr(db, "query", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("connection lost")))
        d = inbound_routing.route_inbound_call(db, to_number=number, at=OPEN_AT)
    finally:
        db.close()
    assert d.status == "error" and d.target == "closed" and d.agent_id != normal


@pytest.mark.parametrize("payload", [None, [], {}, {"call_inbound": None}, {"call_inbound": {}}, {"call_inbound": {"to_number": 5}}, "text"])
def test_a_malformed_payload_is_closed_not_normal(payload):
    _org, number, normal, _c = _setup()
    r = _signed(payload) if payload is not None else TestClient(app).post(
        "/webhooks/retell/inbound", content=b"not json", headers=_hdr(b"not json"))
    assert r.status_code == 200
    out = r.json()["call_inbound"]
    assert out["metadata"]["atla_route"] == "error" and "override_agent_id" not in out


def _hdr(body):
    ts = int(time.time() * 1000)
    return {"x-retell-signature": f"v={ts},d={hmac.new(KEY.encode(), body + str(ts).encode(), hashlib.sha256).hexdigest()}"}


def test_an_unknown_number_gets_no_override():
    out = _retell("+15559998888")["call_inbound"]
    assert "override_agent_id" not in out and out["metadata"]["atla_route"] == "unknown_number"


def test_flag_off_changes_nothing_for_anyone(monkeypatch):
    monkeypatch.setattr(settings, "AFTER_HOURS_ROUTING_ENABLED", False)
    _org, number, *_ = _setup()
    out = _retell(number, CLOSED_AT)["call_inbound"]
    assert out["metadata"]["atla_route"] == "not_enabled" and "override_agent_id" not in out


def test_a_closed_decision_never_carries_the_normal_agent_id_by_construction():
    from app.services.inbound_routing import RouteDecision
    import inspect
    src = inspect.getsource(inbound_routing._decide)
    assert src.count("agent.retell_agent_id") == 2  # the guard and the single open return
    assert RouteDecision("open", "normal", "a", "r").target == "normal"


# ---- signature (separate from the routing tests: RETELL adapter only) ----

PAYLOAD = {"event": "call_inbound", "call_inbound": {"agent_id": "x", "from_number": "+15550001111", "to_number": "+15550009999"}}


@pytest.mark.parametrize("make", [
    lambda: _signed(PAYLOAD, key="wrong-key"),
    lambda: _signed(PAYLOAD, ts=int((time.time() - 3600) * 1000)),
    lambda: _signed(PAYLOAD, tamper=True),
    lambda: TestClient(app).post("/webhooks/retell/inbound", json=PAYLOAD),
    lambda: TestClient(app).post("/webhooks/retell/inbound", json=PAYLOAD, headers={"x-retell-signature": "garbage"}),
], ids=["wrong-key", "stale", "tampered", "unsigned", "malformed-header"])
def test_unsigned_or_badly_signed_inbound_requests_get_a_bare_401(make):
    r = make()
    assert r.status_code == 401 and r.content == b""


def test_a_valid_signature_is_accepted():
    assert _signed(PAYLOAD).status_code == 200


def test_the_simulated_route_does_not_exist_outside_the_local_sandbox(monkeypatch):
    app.dependency_overrides[get_current_user] = lambda: _fake_user()
    try:
        r = TestClient(app).post("/api/v1/dev/simulate-inbound", json={"to_number": "+15550009999"})
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert r.status_code == 404  # unmetered_test_calls_active is false in the test environment


def test_the_simulated_route_requires_a_signed_in_user(sandbox):
    app.dependency_overrides.pop(get_current_user, None)
    assert TestClient(app).post("/api/v1/dev/simulate-inbound", json={"to_number": "+15550009999"}).status_code in (401, 403)


# ======================================================== lead + notification pipeline (shared by every adapter)

def _analyzed_at(org, agent_id, number, when_utc, agent_key="retell_agent_id"):
    cid = _cid()
    analysis = {"call_summary": "Burst pipe.", "custom_analysis_data": LEAD}
    resp = _post(_event("call_analyzed", cid, number, {"retell_agent_id": agent_id}, call_status="ended",
                        call_analysis=analysis, start_timestamp=int(when_utc.timestamp() * 1000)))
    assert resp.status_code == 204
    return cid


def _flag(cid):
    db = SessionLocal()
    try:
        from app.db.models.calls import Call
        call = db.query(Call).filter(Call.retell_call_id == cid).one()
        lead = db.query(Lead).filter(Lead.call_id == call.id).one()
        return lead.received_after_hours
    finally:
        db.close()


def test_a_call_that_began_after_hours_is_flagged_and_the_owner_email_says_so():
    org, number, normal, closed = _setup()
    cid = _analyzed_at(org, normal, number, CLOSED_AT)
    assert _flag(cid) is True
    _send()
    (_to, subject, body), = [m for m in _mail_for(org) if m[1].startswith("New lead")]
    assert "After hours: this call came in while your business was closed." in body


def test_a_call_that_began_during_hours_is_not_flagged_and_the_email_is_unchanged():
    org, number, normal, _c = _setup()
    cid = _analyzed_at(org, normal, number, OPEN_AT)
    assert _flag(cid) is False
    _send()
    (_to, _s, body), = [m for m in _mail_for(org) if m[1].startswith("New lead")]
    assert "After hours" not in body and "Received:" in body


def test_a_business_without_a_schedule_gets_no_classification_at_all():
    org, number, normal, _c = _setup(schedule=None)
    cid = _analyzed_at(org, normal, number, CLOSED_AT)
    assert _flag(cid) is None
    _send()
    (_to, _s, body), = [m for m in _mail_for(org) if m[1].startswith("New lead")]
    assert "After hours" not in body


def test_the_flag_uses_the_call_start_not_the_time_the_webhook_arrives():
    org, number, normal, _c = _setup()  # "now" for the webhook is real time, which is arbitrary
    assert _flag(_analyzed_at(org, normal, number, OPEN_AT)) is False
    assert _flag(_analyzed_at(org, normal, number, CLOSED_AT)) is True


def test_changing_the_schedule_later_does_not_rewrite_history():
    org, number, normal, _c = _setup()
    cid = _analyzed_at(org, normal, number, CLOSED_AT)
    assert _flag(cid) is True
    db = SessionLocal()
    try:
        biz = db.query(Business).filter(Business.organization_id == uuid.UUID(org)).one()
        biz.hours_schedule = {"enabled": True, "weekly": {d: [["00:00", "24:00"]] for d in hs.DAYS}}  # now open 24/7
        db.commit()
    finally:
        db.close()
    assert _flag(cid) is True


def test_calls_answered_by_the_twin_belong_to_the_same_receptionist():
    org, number, normal, closed = _setup()
    cid = _analyzed_at(org, closed, number, CLOSED_AT)  # Retell reports the TWIN's agent id for this call
    db = SessionLocal()
    try:
        from app.db.models.calls import Call
        call = db.query(Call).filter(Call.retell_call_id == cid).one()
        agent = db.query(Agent).filter(Agent.organization_id == uuid.UUID(org)).one()
        assert call.agent_id == agent.id and str(call.organization_id) == org
    finally:
        db.close()
    assert _flag(cid) is True


# ======================================================== twin lifecycle and prompt

from app.providers.agent.agent_provider import AgentProvider  # noqa: E402
from app.services.agent_service import build_agent_config, sync_agent_to_provider  # noqa: E402
from app.services.prompt_builder import build_agent_prompt  # noqa: E402


class RecordingAgents(AgentProvider):
    def __init__(self):
        self.created, self.updated = [], []
        self._n = 0

    def _id(self, p):
        self._n += 1
        return f"{p}_{uuid.uuid4().hex[:6]}_{self._n}"

    def create_llm(self, config):
        self.created.append(("llm", config))
        return self._id("llm")

    def update_llm(self, llm_id, config):
        self.updated.append(("llm", llm_id, config))

    def create_agent(self, config, llm_id):
        self.created.append(("agent", config))
        return self._id("agent")

    def update_agent(self, agent_id, config, llm_id):
        self.updated.append(("agent", agent_id, config))

    def delete_agent(self, agent_id): ...
    def delete_llm(self, llm_id): ...


def _fresh_agent(schedule, transfer=True):
    org, number, normal, closed = _setup(schedule=schedule, twin=False)
    db = SessionLocal()
    agent = db.query(Agent).filter(Agent.organization_id == uuid.UUID(org)).one()
    agent.retell_agent_id = agent.retell_llm_id = None
    agent.tasks = {"answer_questions": True, "capture_leads": True, "transfer_calls": transfer}
    agent.transfer_number = "+15551234567" if transfer else None
    from app.db.models.voice_agent import Voice
    voice = db.get(Voice, agent.voice_id)
    db.commit()
    return db, agent, db.get(Business, agent.business_id), voice


def test_the_closed_twin_has_no_transfer_number_or_instruction_and_the_normal_agent_keeps_both():
    db, agent, biz, voice = _fresh_agent(WEEKDAY)
    try:
        prov = RecordingAgents()
        sync_agent_to_provider(db, agent, biz, voice, prov)
        configs = [c for kind, c in prov.created if kind == "agent"]
        llms = [c for kind, c in prov.created if kind == "llm"]
        assert len(llms) == 2 and len(configs) == 2
        normal, twin = llms
        assert normal.transfer_number == "+15551234567" and twin.transfer_number is None
        assert "transfer them using the transfer tool" in normal.general_prompt
        assert "transfer them using the transfer tool" not in twin.general_prompt and "AFTER HOURS" in twin.general_prompt
        assert "AFTER HOURS" not in normal.general_prompt
        assert agent.closed_retell_agent_id and agent.closed_retell_llm_id and agent.retell_agent_id != agent.closed_retell_agent_id
        assert twin.begin_message == normal.begin_message and twin.voice_provider_id == normal.voice_provider_id
    finally:
        db.close()


def test_resyncing_updates_the_twin_in_place_and_never_creates_a_second_one():
    db, agent, biz, voice = _fresh_agent(WEEKDAY)
    try:
        prov = RecordingAgents()
        sync_agent_to_provider(db, agent, biz, voice, prov)
        twin_agent, twin_llm = agent.closed_retell_agent_id, agent.closed_retell_llm_id
        sync_agent_to_provider(db, agent, biz, voice, prov)
        assert len(prov.created) == 4 and len(prov.updated) == 4
        assert {u[1] for u in prov.updated} >= {twin_agent, twin_llm}
        assert (agent.closed_retell_agent_id, agent.closed_retell_llm_id) == (twin_agent, twin_llm)
    finally:
        db.close()


@pytest.mark.parametrize("schedule", [None, {**WEEKDAY, "enabled": False}], ids=["no-schedule", "disabled"])
def test_a_business_that_has_not_opted_in_gets_no_twin_and_an_unchanged_prompt(schedule):
    db, agent, biz, voice = _fresh_agent(schedule)
    try:
        prov = RecordingAgents()
        sync_agent_to_provider(db, agent, biz, voice, prov)
        assert [k for k, _ in prov.created] == ["llm", "agent"] and agent.closed_retell_agent_id is None
        assert "AFTER HOURS" not in prov.created[0][1].general_prompt
    finally:
        db.close()


def test_with_the_flag_off_no_twin_is_created_even_for_an_opted_in_business(monkeypatch):
    monkeypatch.setattr(settings, "AFTER_HOURS_ROUTING_ENABLED", False)
    db, agent, biz, voice = _fresh_agent(WEEKDAY)
    try:
        prov = RecordingAgents()
        sync_agent_to_provider(db, agent, biz, voice, prov)
        assert len(prov.created) == 2 and agent.closed_retell_agent_id is None
    finally:
        db.close()


def test_the_closed_prompt_forbids_transfers_even_if_a_transfer_number_is_configured():
    db, agent, biz, voice = _fresh_agent(WEEKDAY)
    try:
        cfg = build_agent_config(db, agent, biz, voice, closed=True)
        assert cfg.transfer_number is None and "do not offer, attempt or promise a transfer" in cfg.general_prompt
        assert build_agent_config(db, agent, biz, voice).transfer_number == "+15551234567"
    finally:
        db.close()


def test_the_default_prompt_is_byte_identical_when_after_hours_is_not_requested():
    kw = dict(business_name="B", industry=None, address=None, phone=None, website=None, description=None,
              hours_text="Mon: 9-5", personality="friendly", tasks={"transfer_calls": True}, has_transfer=True, knowledge_text="k")
    assert build_agent_prompt(**kw) == build_agent_prompt(**kw, after_hours=False)
    assert "transfer them using the transfer tool" in build_agent_prompt(**kw)


# ======================================================== activation: default agent is the twin

def _activation_setup(schedule, twin=True):
    org, number, normal, closed = _setup(schedule=schedule, twin=twin)
    db = SessionLocal()
    phone = db.query(PhoneNumber).filter(PhoneNumber.organization_id == uuid.UUID(org)).one()
    agent = db.query(Agent).filter(Agent.organization_id == uuid.UUID(org)).one()
    prov = FakePhone()
    prov.numbers[phone.retell_phone_number_id] = {"nickname": "n", "agent": None}
    return db, org, agent, phone, prov, normal, closed


def test_an_opted_in_number_defaults_to_the_twin_and_registers_the_inbound_webhook():
    db, org, agent, phone, prov, normal, closed = _activation_setup(WEEKDAY)
    try:
        activate_receptionist(db, uuid.UUID(org), agent.id, phone.id, prov)
        assert prov.numbers[phone.retell_phone_number_id]["agent"] == closed
        assert prov.webhooks[phone.retell_phone_number_id].endswith("/webhooks/retell/inbound")
        assert verify_receptionist_live(db, uuid.UUID(org), agent, phone, prov)[0] == "live"
        prov.numbers[phone.retell_phone_number_id]["agent"] = normal  # someone pointed the number at the transfer agent
        assert verify_receptionist_live(db, uuid.UUID(org), agent, phone, prov)[0] == "not_live"
    finally:
        db.close()


def test_a_business_that_never_opted_in_is_activated_exactly_as_before():
    db, org, agent, phone, prov, normal, closed = _activation_setup(None)
    try:
        activate_receptionist(db, uuid.UUID(org), agent.id, phone.id, prov)
        assert prov.numbers[phone.retell_phone_number_id]["agent"] == normal and prov.webhooks == {}
        assert verify_receptionist_live(db, uuid.UUID(org), agent, phone, prov)[0] == "live"
    finally:
        db.close()


def test_going_live_is_refused_when_routing_needs_a_twin_that_does_not_exist():
    db, org, agent, phone, prov, *_ = _activation_setup(WEEKDAY, twin=False)
    try:
        with pytest.raises(ActivationBlocked):
            activate_receptionist(db, uuid.UUID(org), agent.id, phone.id, prov)
        assert prov.assigns == [] and prov.webhooks == {}  # nothing was pointed at the normal agent
    finally:
        db.close()


def test_opting_out_and_going_live_again_restores_the_normal_agent_and_clears_the_webhook():
    db, org, agent, phone, prov, normal, closed = _activation_setup(WEEKDAY)
    try:
        activate_receptionist(db, uuid.UUID(org), agent.id, phone.id, prov)
        biz = db.get(Business, agent.business_id)
        biz.hours_schedule = {**WEEKDAY, "enabled": False}
        db.commit()
        activate_receptionist(db, uuid.UUID(org), agent.id, phone.id, prov)
        assert prov.numbers[phone.retell_phone_number_id]["agent"] == normal
        assert prov.webhooks[phone.retell_phone_number_id] is None
    finally:
        db.close()
