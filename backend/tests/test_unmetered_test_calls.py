"""
ALLOW_UNMETERED_LOCAL_TEST_CALLS: a local-sandbox-only way to run browser test calls on a cloned database that has no
credits. The flag does nothing unless ENV=development AND PADDLE_ENV=sandbox AND the database is exactly
atla_paddle_sandbox_clone; it never writes credits, subscriptions or billing rows; and everything else about a test
call (auth, membership, agent ownership and sync, rate limit, duration cap) is unchanged.
"""
import uuid

import pytest
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.billing import CreditLedgerEntry, FreeTestAllowance, Subscription, UsageLedgerEntry
from app.db.models.calls import Call
from app.db.models.voice_agent import Agent
from app.services.credits import credit_summary
from app.services.plans import PLANS, Plan
from tests.billing_helpers import add_usage, new_org, subscribe
from tests.test_agent_config import _agent_id, _body, _put, _seed_business, fake  # noqa: F401  (fake is a fixture)
from tests.test_call_pipeline import _call, _post as retell_post

settings = get_settings()
CLONE = "atla_paddle_sandbox_clone"


def _url_with_db(name: str, host: str | None = None, query: dict | None = None) -> str:
    url = make_url(settings.DATABASE_URL).set(database=name)
    if host is not None:
        url = url.set(host=host)
    if query:
        url = url.set(query=query)
    return url.render_as_string(hide_password=False)


def _configure(monkeypatch, *, env="development", paddle="sandbox", db=CLONE, flag=True, host=None, query=None):
    monkeypatch.setattr(settings, "ENV", env)
    monkeypatch.setattr(settings, "PADDLE_ENV", paddle)
    monkeypatch.setattr(settings, "DATABASE_URL", _url_with_db(db, host, query))
    monkeypatch.setattr(settings, "ALLOW_UNMETERED_LOCAL_TEST_CALLS", flag)


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    monkeypatch.setattr(settings, "FREE_TEST_CALLS_PER_ORG", 1)
    monkeypatch.setattr(settings, "TEST_CALLS_PER_DAY_PER_ORG", 1000)
    monkeypatch.setattr(settings, "RETELL_API_KEY", "test-retell-key")


def _ready_org():
    c, org = new_org("um")
    biz, voice = _seed_business(org)
    assert _put(c, org, biz, _body(voice)).status_code == 200
    return c, org, f"/api/v1/orgs/{org}/agents/{_agent_id(c, org)}/test-call"


def _no_credits_subscriber(monkeypatch):
    """A subscribed org whose included minutes are all spent: today's 402 'out of credits' case."""
    monkeypatch.setitem(PLANS, "starter", Plan("starter", "Starter", 1, PLANS["starter"].features, 1))
    c, org, url = _ready_org()
    subscribe(org)
    add_usage(org, 10 * 60)
    return c, org, url


def _billing_snapshot(org):
    db = SessionLocal()
    try:
        o = uuid.UUID(org)
        subs = [(s.id, s.status, s.plan_id, s.current_period_end) for s in db.query(Subscription).filter_by(organization_id=o)]
        credits = db.query(CreditLedgerEntry).filter_by(organization_id=o).count()
        allowances = db.query(FreeTestAllowance).filter_by(organization_id=o).count()
        return sorted(subs, key=str), credits, allowances
    finally:
        db.close()


def _available(org):
    db = SessionLocal()
    try:
        return credit_summary(db, uuid.UUID(org)).available_seconds
    finally:
        db.close()


# ---------------- the guard: which configurations may turn it on ----------------

def test_off_by_default():
    assert get_settings().__class__.model_fields["ALLOW_UNMETERED_LOCAL_TEST_CALLS"].default is False


def test_all_three_conditions_together_allow_it(monkeypatch):
    _configure(monkeypatch)
    assert settings.unmetered_test_call_problems() == [] and settings.unmetered_test_calls_active is True
    settings.assert_startup_safe("api")  # does not raise


@pytest.mark.parametrize(
    "kw",
    [
        dict(env="test"),
        dict(env="production"),
        dict(paddle="live"),
        dict(db="atla"),
        dict(db="atla_test"),
        dict(db="atla_paddle_sandbox"),
        dict(db="atla_paddle_sandbox_clone2"),
        dict(db="atla_paddle_sandbox_clone_test"),
        dict(db="xatla_paddle_sandbox_clone"),
        dict(db="ATLA_PADDLE_SANDBOX_CLONE"),
    ],
)
def test_any_other_environment_or_database_cannot_enable_it_and_refuses_startup(monkeypatch, kw):
    _configure(monkeypatch, **kw)
    assert settings.unmetered_test_calls_active is False
    assert settings.unmetered_test_call_problems()
    for process in ("api", "worker"):
        with pytest.raises(RuntimeError, match="ALLOW_UNMETERED_LOCAL_TEST_CALLS"):
            settings.assert_startup_safe(process)


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "127.0.0.1", "::1"])
def test_loopback_hosts_with_the_clone_name_are_allowed(monkeypatch, host):
    _configure(monkeypatch, host=host)
    assert settings.unmetered_test_call_problems() == [] and settings.unmetered_test_calls_active is True
    settings.assert_startup_safe("api")


@pytest.mark.parametrize(
    "host",
    ["db.example.com", "10.0.0.5", "192.168.1.20", "127.0.0.2", "0.0.0.0", "localhost.evil.com", "prod-db.internal", "::2", "localhost,db.example.com"],
)
def test_a_remote_host_is_refused_even_when_the_database_name_matches(monkeypatch, host):
    _configure(monkeypatch, host=host)
    assert settings.unmetered_test_calls_active is False
    assert any("host must be" in p for p in settings.unmetered_test_call_problems())
    with pytest.raises(RuntimeError, match="host must be"):
        settings.assert_startup_safe("api")


@pytest.mark.parametrize("query", [{"host": "db.example.com"}, {"hostaddr": "10.0.0.5"}, {"service": "prod"}, {"dbname": "atla"}])
def test_a_libpq_query_override_cannot_smuggle_in_a_remote_server(monkeypatch, query):
    _configure(monkeypatch, host="localhost", query=query)  # URL host is loopback, but libpq would connect elsewhere
    assert settings.unmetered_test_calls_active is False
    with pytest.raises(RuntimeError, match="ALLOW_UNMETERED_LOCAL_TEST_CALLS"):
        settings.assert_startup_safe("worker")


def test_a_missing_host_unix_socket_is_not_accepted(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(settings, "DATABASE_URL", f"postgresql+psycopg:///{CLONE}")
    assert settings.unmetered_test_calls_active is False


def test_a_remote_host_cannot_start_an_unmetered_call(fake, monkeypatch):  # noqa: F811
    c, org, url = _no_credits_subscriber(monkeypatch)
    _configure(monkeypatch, host="db.example.com")
    resp = c.post(url)
    assert resp.status_code == 402 and len(fake.to("/v3/create-web-call")) == 0


def test_the_flag_off_never_blocks_startup_in_any_environment(monkeypatch):
    _configure(monkeypatch, env="test", db="atla_test", flag=False)
    settings.assert_startup_safe("api")
    assert settings.unmetered_test_calls_active is False


def test_an_unparseable_database_url_is_never_treated_as_the_clone(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(settings, "DATABASE_URL", "not a url")
    assert settings.unmetered_test_calls_active is False


# ---------------- the endpoint ----------------

def test_local_clone_with_the_flag_can_test_despite_zero_credits(fake, monkeypatch):  # noqa: F811
    c, org, url = _no_credits_subscriber(monkeypatch)
    before = _billing_snapshot(org)
    avail = _available(org)
    _configure(monkeypatch)
    resp = c.post(url)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_free"] is False and body["max_seconds"] == settings.TEST_CALL_MAX_SECONDS
    assert len(fake.to("/v3/create-web-call")) == 1
    assert fake.to("/v3/create-web-call")[-1][2]["metadata"]["kind"] == "dev_test"
    assert _billing_snapshot(org) == before  # no credits granted, no subscription or allowance row touched
    assert _available(org) == avail


@pytest.mark.parametrize("kw", [dict(db="atla"), dict(db="atla_test"), dict(env="production"), dict(env="test"), dict(paddle="live")])
def test_zero_credits_still_402_when_any_condition_fails_even_with_the_flag(fake, monkeypatch, kw):  # noqa: F811
    c, org, url = _no_credits_subscriber(monkeypatch)
    _configure(monkeypatch, **kw)
    resp = c.post(url)
    assert resp.status_code == 402 and "out of credits" in resp.json()["detail"].lower()
    assert len(fake.to("/v3/create-web-call")) == 0


def test_sandbox_clone_with_the_flag_disabled_keeps_existing_behavior(fake, monkeypatch):  # noqa: F811
    c, org, url = _no_credits_subscriber(monkeypatch)
    _configure(monkeypatch, flag=False)
    resp = c.post(url)
    assert resp.status_code == 402 and "out of credits" in resp.json()["detail"].lower()
    assert len(fake.to("/v3/create-web-call")) == 0


def test_the_free_call_is_used_first_then_the_exhausted_allowance_is_bypassed(fake, monkeypatch):  # noqa: F811
    c, org, url = _ready_org()  # not subscribed
    _configure(monkeypatch)
    first = c.post(url)
    assert first.status_code == 200 and first.json()["is_free"] is True
    second = c.post(url)
    assert second.status_code == 200 and second.json()["is_free"] is False
    assert _billing_snapshot(org)[2] == 1  # still exactly the one real allowance row, never inflated or reset
    assert fake.to("/v3/create-web-call")[-1][2]["metadata"]["kind"] == "dev_test"


def test_an_exhausted_free_call_is_still_refused_without_the_bypass(fake, monkeypatch):  # noqa: F811
    c, org, url = _ready_org()
    assert c.post(url).status_code == 200
    _configure(monkeypatch, db="atla_test")
    assert c.post(url).status_code == 402


def test_a_lapsed_plan_is_still_refused_with_the_bypass(fake, monkeypatch):  # noqa: F811
    c, org, url = _ready_org()
    subscribe(org, status="past_due")
    _configure(monkeypatch)
    resp = c.post(url)
    assert resp.status_code == 402 and "billing" in resp.json()["detail"].lower()
    assert len(fake.to("/v3/create-web-call")) == 0


# ---------------- everything else is still enforced ----------------

def test_authentication_is_still_required(fake, monkeypatch):  # noqa: F811
    _c, _org, url = _ready_org()
    _configure(monkeypatch)
    from fastapi.testclient import TestClient

    from app.main import app

    assert TestClient(app).post(url).status_code == 401
    assert len(fake.to("/v3/create-web-call")) == 0


def test_membership_and_agent_ownership_are_still_enforced(fake, monkeypatch):  # noqa: F811
    _c, _org, url = _ready_org()
    other, _org2 = new_org("um")
    _configure(monkeypatch)
    assert other.post(url).status_code == 404
    assert len(fake.to("/v3/create-web-call")) == 0


def test_an_unsynced_agent_is_still_refused(fake, monkeypatch):  # noqa: F811
    c, org, url = _ready_org()
    db = SessionLocal()
    try:
        db.query(Agent).filter(Agent.organization_id == uuid.UUID(org)).update({"version": Agent.version + 1})
        db.commit()
    finally:
        db.close()
    _configure(monkeypatch)
    assert c.post(url).status_code == 409
    assert len(fake.to("/v3/create-web-call")) == 0


def test_the_daily_rate_limit_is_still_enforced(fake, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "TEST_CALLS_PER_DAY_PER_ORG", 2)
    c, org, url = _no_credits_subscriber(monkeypatch)
    _configure(monkeypatch)
    assert c.post(url).status_code == 200
    assert c.post(url).status_code == 200
    third = c.post(url)
    assert third.status_code == 429
    assert len(fake.to("/v3/create-web-call")) == 2
    db = SessionLocal()
    try:  # the refused attempt left no stray in-flight call behind
        assert db.query(Call).filter(Call.organization_id == uuid.UUID(org), Call.status == "registered").count() == 2
    finally:
        db.close()


# ---------------- usage accounting ----------------

def _end(call_id, fake, **over):  # noqa: F811
    retell_post("call_ended", _call(call_id, fake.ids["agent"], None, call_type="web_call", direction=None, from_number=None, **over))


def test_an_unmetered_call_is_recorded_with_its_real_cost_as_its_own_kind_and_is_never_charged(fake, monkeypatch):  # noqa: F811
    c, org, url = _no_credits_subscriber(monkeypatch)
    avail = _available(org)
    remaining = c.get(f"/api/v1/orgs/{org}/usage/credits").json()["free_test_calls_remaining"]
    _configure(monkeypatch)
    call_id = c.post(url).json()["call_id"]
    _end(call_id, fake, duration_ms=90_000, call_cost={"combined_cost": 12.13})
    db = SessionLocal()
    try:
        row = db.query(UsageLedgerEntry).filter_by(retell_call_id=call_id).one()
        assert row.kind == "dev_test"  # neither "test" (customer-paid) nor "free_test" (promotional)
        assert float(row.provider_cost) == 12.13 and row.billable_seconds == 90
    finally:
        db.close()
    assert _available(org) == avail  # credits untouched: the call was not charged
    usage = c.get(f"/api/v1/orgs/{org}/usage/credits")
    assert usage.status_code == 200 and usage.json()["free_test_calls_remaining"] == remaining  # the promo allowance was not consumed


def test_admission_is_durable_a_late_webhook_stays_dev_test_after_the_flag_is_turned_off(fake, monkeypatch):  # noqa: F811
    c, org, url = _no_credits_subscriber(monkeypatch)
    avail = _available(org)
    before = _billing_snapshot(org)
    _configure(monkeypatch)
    call_id = c.post(url).json()["call_id"]
    db = SessionLocal()
    try:
        assert db.query(Call).filter_by(retell_call_id=call_id).one().admitted_as == "dev_test"  # persisted at admission
    finally:
        db.close()
    # The environment changes before the webhook arrives: flag off, then a different database / production entirely.
    for kw in (dict(flag=False), dict(env="production", db="atla", flag=False)):
        _configure(monkeypatch, **kw)
        assert settings.unmetered_test_calls_active is False
    _end(call_id, fake, duration_ms=120_000, call_cost={"combined_cost": 20.0})
    db = SessionLocal()
    try:
        row = db.query(UsageLedgerEntry).filter_by(retell_call_id=call_id).one()
        assert row.kind == "dev_test" and row.billable_seconds == 120 and float(row.provider_cost) == 20.0
    finally:
        db.close()
    assert _available(org) == avail  # no credits charged
    assert _billing_snapshot(org) == before


def test_a_duplicate_late_webhook_does_not_reclassify(fake, monkeypatch):  # noqa: F811
    c, org, url = _no_credits_subscriber(monkeypatch)
    _configure(monkeypatch)
    call_id = c.post(url).json()["call_id"]
    _configure(monkeypatch, flag=False)
    _end(call_id, fake, duration_ms=30_000)
    _end(call_id, fake, duration_ms=31_000)
    db = SessionLocal()
    try:
        rows = db.query(UsageLedgerEntry).filter_by(retell_call_id=call_id).all()
        assert len(rows) == 1 and rows[0].kind == "dev_test"
    finally:
        db.close()


def test_calls_admitted_normally_never_carry_the_marker(fake, monkeypatch):  # noqa: F811
    c, org, url = _ready_org()
    subscribe(org)
    call_id = c.post(url).json()["call_id"]  # no flag: a normal paid test call
    db = SessionLocal()
    try:
        assert db.query(Call).filter_by(retell_call_id=call_id).one().admitted_as is None
    finally:
        db.close()
    _end(call_id, fake, duration_ms=60_000)
    db = SessionLocal()
    try:
        assert db.query(UsageLedgerEntry).filter_by(retell_call_id=call_id).one().kind == "test"
    finally:
        db.close()


def test_a_free_call_admitted_under_the_flag_is_not_marked_dev_test(fake, monkeypatch):  # noqa: F811
    c, org, url = _ready_org()
    _configure(monkeypatch)
    call_id = c.post(url).json()["call_id"]
    db = SessionLocal()
    try:
        assert db.query(Call).filter_by(retell_call_id=call_id).one().admitted_as is None
    finally:
        db.close()


def test_the_normal_free_call_is_still_classified_free_test_under_the_flag(fake, monkeypatch):  # noqa: F811
    c, org, url = _ready_org()
    _configure(monkeypatch)
    call_id = c.post(url).json()["call_id"]
    _end(call_id, fake, duration_ms=45_000, call_cost={"combined_cost": 5.0})
    db = SessionLocal()
    try:
        assert db.query(UsageLedgerEntry).filter_by(retell_call_id=call_id).one().kind == "free_test"
    finally:
        db.close()


def test_a_production_call_is_unaffected_by_the_flag(fake, monkeypatch):  # noqa: F811
    c, org, url = _ready_org()
    subscribe(org)
    from tests.billing_helpers import add_usage as _au  # noqa: F401

    _configure(monkeypatch)
    before = _available(org)
    add_usage(org, 120, kind="production")
    assert _available(org) == before - 120  # production usage is still charged


def test_a_due_plan_grant_is_not_created_by_an_unmetered_test_call(fake, monkeypatch):  # noqa: F811
    c, org, url = _ready_org()
    subscribe(org, grant=False)  # a grant is "due"; the normal path would create it, the bypass must not
    before = _billing_snapshot(org)
    assert before[1] == 0
    _configure(monkeypatch)
    assert c.post(url).status_code == 200
    assert _billing_snapshot(org) == before
