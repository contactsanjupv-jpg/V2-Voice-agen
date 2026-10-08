"""Server-side plan enforcement + plan mapping from Paddle PRICE ids (not client data)."""
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.config import get_settings
from app.services import plans
from app.services.plans import PLANS, Feature, Plan
from tests.test_billing import SECRET, _new_org, _post  # noqa: F401  (SECRET used by fixture below)


@pytest.fixture(autouse=True)
def _paddle_settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "PADDLE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(s, "PADDLE_API_KEY", "test_key")
    monkeypatch.setattr(s, "PADDLE_ENV", "sandbox")
    monkeypatch.setattr(s, "PADDLE_STARTER_PRICE_ID", "pri_starter")
    monkeypatch.setattr(s, "PADDLE_GROWTH_PRICE_ID", "pri_growth")


def _ev(event_type, sub_id, org_id, price_id=None, status="active", custom_plan=None, at="2026-10-01T00:00:00.000000Z"):
    data = {
        "id": sub_id,
        "status": status,
        "custom_data": {"organization_id": org_id, **({"plan_id": custom_plan} if custom_plan else {})},
        "current_billing_period": {"starts_at": "2026-10-01T00:00:00Z", "ends_at": "2026-11-01T00:00:00Z"},
    }
    if price_id:
        data["items"] = [{"status": "active", "quantity": 1, "price": {"id": price_id}}]
    return {
        "event_id": f"evt_{uuid.uuid4().hex}",
        "event_type": event_type,
        "occurred_at": at,
        "notification_id": f"ntf_{uuid.uuid4().hex}",
        "data": data,
    }


def _plan_of(db, org_id):
    from app.services.billing_state import current_subscription

    db.rollback()
    sub = current_subscription(db, uuid.UUID(org_id))
    return sub.plan_id if sub else None


def _gate(db, org_id, feature):
    dep = __import__("app.auth.deps", fromlist=["require_feature"]).require_feature(feature)
    return dep(membership=SimpleNamespace(organization_id=uuid.UUID(org_id)), db=db)


@pytest.fixture
def tiered_plans(monkeypatch):
    """Registers a Growth-only feature for the test so the Starter/Growth matrix is exercised for real."""
    starter_only, growth_only = "x_starter_feature", "x_growth_feature"
    base = set(plans.STARTER_FEATURES)
    monkeypatch.setitem(PLANS, "starter", Plan("starter", "Starter", 1, frozenset(base | {starter_only})))
    monkeypatch.setitem(PLANS, "growth", Plan("growth", "Growth", 2, frozenset(base | {starter_only, growth_only})))
    return starter_only, growth_only


# ---------- catalog invariants ----------

def test_growth_inherits_every_starter_feature():
    assert PLANS["starter"].features <= PLANS["growth"].features


def test_every_plan_has_a_configured_price():
    for plan_id in PLANS:
        assert plans.price_id_for_plan(plan_id)
    assert plans.plan_id_for_price("pri_starter") == "starter"
    assert plans.plan_id_for_price("pri_growth") == "growth"
    assert plans.plan_id_for_price("pri_other") is None
    assert plans.plan_id_for_price("") is None


# ---------- the entitlement matrix (server-side dependency) ----------

def test_starter_gets_starter_feature_but_not_growth_feature(db, tiered_plans):
    starter_f, growth_f = tiered_plans
    _c, org = _new_org()
    _post(_ev("subscription.created", f"sub_{uuid.uuid4().hex[:12]}", org, "pri_starter"))
    db.rollback()
    assert _gate(db, org, starter_f) is not None
    with pytest.raises(HTTPException) as exc:
        _gate(db, org, growth_f)
    assert exc.value.status_code == 402
    assert exc.value.detail == "This feature is available on Growth. Upgrade to unlock it."


def test_growth_gets_starter_and_growth_features(db, tiered_plans):
    starter_f, growth_f = tiered_plans
    _c, org = _new_org()
    _post(_ev("subscription.created", f"sub_{uuid.uuid4().hex[:12]}", org, "pri_growth"))
    db.rollback()
    assert _gate(db, org, starter_f) is not None
    assert _gate(db, org, growth_f) is not None
    assert _gate(db, org, Feature.phone_number) is not None


def test_no_subscription_is_rejected_for_every_real_feature(db):
    _c, org = _new_org()
    for feature in Feature:
        with pytest.raises(HTTPException) as exc:
            _gate(db, org, feature)
        assert exc.value.status_code == 402


@pytest.mark.parametrize("bad_status", ["past_due", "paused", "canceled"])
def test_lapsed_subscription_loses_features(db, bad_status):
    _c, org = _new_org()
    sub = f"sub_{uuid.uuid4().hex[:12]}"
    _post(_ev("subscription.created", sub, org, "pri_starter"))
    db.rollback()
    assert _gate(db, org, Feature.go_live) is not None
    _post(_ev("subscription.updated", sub, org, "pri_starter", status=bad_status, at="2026-10-02T00:00:00Z"))
    db.rollback()
    with pytest.raises(HTTPException) as exc:
        _gate(db, org, Feature.go_live)
    assert exc.value.status_code == 402


def test_recovery_from_past_due_restores_features(db):
    _c, org = _new_org()
    sub = f"sub_{uuid.uuid4().hex[:12]}"
    _post(_ev("subscription.created", sub, org, "pri_starter"))
    _post(_ev("subscription.updated", sub, org, "pri_starter", status="past_due", at="2026-10-02T00:00:00Z"))
    db.rollback()
    with pytest.raises(HTTPException):
        _gate(db, org, Feature.phone_number)
    _post(_ev("subscription.updated", sub, org, "pri_starter", status="active", at="2026-10-03T00:00:00Z"))
    db.rollback()
    assert _gate(db, org, Feature.phone_number) is not None


def test_entitlements_do_not_leak_between_orgs(db, tiered_plans):
    _starter_f, growth_f = tiered_plans
    _a, org_a = _new_org()
    _b, org_b = _new_org()
    _post(_ev("subscription.created", f"sub_{uuid.uuid4().hex[:12]}", org_a, "pri_growth"))
    db.rollback()
    assert _gate(db, org_a, growth_f) is not None
    with pytest.raises(HTTPException):
        _gate(db, org_b, growth_f)


def test_unrecognised_plan_gets_no_features_even_if_active(db):
    _c, org = _new_org()
    _post(_ev("subscription.created", f"sub_{uuid.uuid4().hex[:12]}", org, "pri_not_ours"))
    assert _plan_of(db, org) == "unknown"
    with pytest.raises(HTTPException) as exc:
        _gate(db, org, Feature.phone_number)
    assert exc.value.status_code == 402


# ---------- endpoints enforce it too (direct API calls, no UI) ----------

def test_phone_and_activate_endpoints_are_402_without_a_plan():
    c, org = _new_org()
    assert c.post(f"/api/v1/orgs/{org}/phone-numbers", json={"country": "US", "area_code": "305"}).status_code == 402
    body = {"agent_id": str(uuid.uuid4()), "phone_number_id": str(uuid.uuid4())}
    assert c.post(f"/api/v1/orgs/{org}/activate", json=body).status_code == 402


def test_unrecognised_plan_is_402_at_the_endpoint():
    c, org = _new_org()
    _post(_ev("subscription.created", f"sub_{uuid.uuid4().hex[:12]}", org, "pri_not_ours"))
    resp = c.post(f"/api/v1/orgs/{org}/phone-numbers", json={"country": "US", "area_code": "305"})
    assert resp.status_code == 402
    assert "plan" in resp.json()["detail"].lower()


# ---------- plan comes from the PRICE, not from client-supplied data ----------

def test_price_beats_custom_data_on_create(db):
    """Paddle.js lets a client pass customData; a Starter price with customData plan=growth must still be Starter."""
    _c, org = _new_org()
    _post(_ev("subscription.created", f"sub_{uuid.uuid4().hex[:12]}", org, "pri_starter", custom_plan="growth"))
    assert _plan_of(db, org) == "starter"


def test_payload_without_items_falls_back_to_custom_data(db):
    _c, org = _new_org()
    _post(_ev("subscription.created", f"sub_{uuid.uuid4().hex[:12]}", org, None, custom_plan="starter"))
    assert _plan_of(db, org) == "starter"


def test_upgrade_and_downgrade_follow_the_price(db):
    _c, org = _new_org()
    sub = f"sub_{uuid.uuid4().hex[:12]}"
    _post(_ev("subscription.created", sub, org, "pri_starter", custom_plan="starter"))
    assert _plan_of(db, org) == "starter"
    _post(_ev("subscription.updated", sub, org, "pri_growth", custom_plan="starter", at="2026-10-02T00:00:00Z"))
    assert _plan_of(db, org) == "growth"
    _post(_ev("subscription.updated", sub, org, "pri_starter", custom_plan="starter", at="2026-10-03T00:00:00Z"))
    assert _plan_of(db, org) == "starter"


def test_stale_plan_change_is_ignored(db):
    _c, org = _new_org()
    sub = f"sub_{uuid.uuid4().hex[:12]}"
    _post(_ev("subscription.created", sub, org, "pri_starter", at="2026-10-01T00:00:00Z"))
    _post(_ev("subscription.updated", sub, org, "pri_growth", at="2026-10-03T00:00:00Z"))
    _post(_ev("subscription.updated", sub, org, "pri_starter", at="2026-10-02T00:00:00Z"))  # late, older
    assert _plan_of(db, org) == "growth"


def test_mixed_or_foreign_prices_fail_closed():
    assert plans.plan_id_from_items([{"price": {"id": "pri_starter"}}, {"price": {"id": "pri_other"}}]) == "unknown"
    assert plans.plan_id_from_items([{"price": {"id": "pri_starter"}}, {"price": {"id": "pri_growth"}}]) == "unknown"
    assert plans.plan_id_from_items([]) is None
    assert plans.plan_id_from_items(None) is None