"""Notification preferences (Task 10): per person, per category, per organization, in-app only. A mute stops NEW notifications of
that category being created for that person; critical ones are always delivered; history is untouched; nothing here is email."""
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.db.base import SessionLocal
from app.db.models.notifications import CATEGORIES, Notification, NotificationPreference
from app.db.models.tenancy import Organization, OrganizationMember, OrgRole
from app.main import app
from app.services import notifications as svc
from tests.test_notifications import TestClientFor, _add_user, _make, _org, _owner_id, _rows, _url  # noqa: F401

CRITICAL_CATEGORIES = {"receptionist", "credits", "billing", "phone"}  # the categories that have a critical kind in the catalogue


def _prefs(client, org):
    r = client.get(_url(org, "/preferences"))
    assert r.status_code == 200, r.text
    return {p["category"]: p for p in r.json()}


def _set(client, org, category, enabled):
    return client.put(_url(org, f"/preferences/{category}"), json={"enabled": enabled})


def _kinds(org, user_id):
    return [r.kind for r in _rows(org, user_id)]


# ------------------------------------------------------------------ defaults and reading
def test_everything_is_on_by_default_and_the_critical_categories_are_flagged():
    client, org = _org()
    prefs = _prefs(client, org)
    assert list(prefs) == list(CATEGORIES)
    assert all(p["enabled"] for p in prefs.values())
    assert {c for c, p in prefs.items() if p["critical_always_delivered"]} == CRITICAL_CATEGORIES
    assert set(next(iter(prefs.values()))) == {"category", "enabled", "critical_always_delivered"}  # no email field exists


def test_the_critical_flag_matches_the_catalogue():
    assert {c for c, sev, _l, _a in svc.CATALOGUE.values() if sev == "critical"} == CRITICAL_CATEGORIES


# ------------------------------------------------------------------ muting and unmuting
def test_a_muted_category_stops_new_notifications_for_that_person_only():
    client, org = _org()
    _a, admin = _add_user(org, OrgRole.admin)
    owner = _owner_id(org)
    assert _set(client, org, "leads", False).json() == {"category": "leads", "enabled": False, "critical_always_delivered": False}
    assert _prefs(client, org)["leads"]["enabled"] is False
    assert _make(org, "lead", "L1", {"name": "Pat"}) is True  # the admin still gets it, so the call created a row
    assert "lead" not in _kinds(org, owner) and "lead" in _kinds(org, admin)


def test_unmuting_resumes_delivery_and_a_missed_notification_can_still_be_created_later_under_the_same_key():
    client, org = _org()
    owner = _owner_id(org)
    _set(client, org, "leads", False)
    assert _make(org, "lead", "K", {"name": "Pat"}) is False  # nobody left to tell: nothing created
    assert _rows(org, owner) == []
    _set(client, org, "leads", True)
    assert _make(org, "lead", "K", {"name": "Pat"}) is True  # the dedupe key was never consumed by the muted attempt
    assert _kinds(org, owner) == ["lead"]


def test_muting_never_removes_or_hides_existing_history_or_changes_the_unread_count():
    client, org = _org()
    _make(org, "lead", "H1", {"name": "Pat"})
    before = client.get(_url(org)).json()
    _set(client, org, "leads", False)
    after = client.get(_url(org)).json()
    assert after == before and after["unread_count"] == 1 and len(after["items"]) == 1


def test_critical_notifications_are_delivered_even_when_their_category_is_muted():
    client, org = _org()
    owner = _owner_id(org)
    _set(client, org, "billing", False)
    _make(org, "plan_changed", "P1", {"plan_id": "starter"})  # billing, info: muted
    _make(org, "payment_failed", "F1")  # billing, CRITICAL: delivered
    assert _kinds(org, owner) == ["payment_failed"]
    _set(client, org, "credits", False)
    _make(org, "low_credits", "C1", {"balance_minutes": 3, "threshold_minutes": 10})  # credits, warning: muted
    _make(org, "credits_empty", "C2")  # credits, critical: delivered
    assert _kinds(org, owner) == ["payment_failed", "credits_empty"]


@pytest.mark.parametrize("category", list(CATEGORIES))
def test_every_category_can_be_muted_and_unmuted_through_the_api(category):
    client, org = _org()
    assert _set(client, org, category, False).json()["enabled"] is False
    assert _prefs(client, org)[category]["enabled"] is False and sum(not p["enabled"] for p in _prefs(client, org).values()) == 1
    assert _set(client, org, category, True).json()["enabled"] is True
    assert all(p["enabled"] for p in _prefs(client, org).values())


def test_analytics_and_calls_notifications_honour_the_mute():
    client, org = _org()
    owner = _owner_id(org)
    _set(client, org, "analytics", False)
    db = SessionLocal()
    try:
        assert svc.create(db, uuid.UUID(org), "daily_report", "d1", {"date": "2026-11-23", "calls": 1, "leads": 0}) is False
        db.commit()
    finally:
        db.close()
    assert _rows(org, owner) == []


# ------------------------------------------------------------------ isolation and authorization
def test_one_persons_choice_never_changes_anyone_elses():
    client, org = _org()
    admin_client, admin = _add_user(org, OrgRole.admin)
    _set(admin_client, org, "leads", False)
    assert _prefs(client, org)["leads"]["enabled"] is True and _prefs(admin_client, org)["leads"]["enabled"] is False
    _make(org, "lead", "L1", {"name": "Pat"})
    assert "lead" in _kinds(org, _owner_id(org)) and "lead" not in _kinds(org, admin)


def test_a_person_in_two_organizations_has_separate_choices_for_each():
    client_a, org_a = _org()
    _client_b, org_b = _org()
    owner_a = _owner_id(org_a)
    db = SessionLocal()
    try:
        db.add(OrganizationMember(organization_id=uuid.UUID(org_b), user_id=owner_a, role=OrgRole.admin))
        db.commit()
    finally:
        db.close()
    _set(client_a, org_a, "leads", False)
    assert _prefs(client_a, org_b)["leads"]["enabled"] is True  # same person, other organization: unaffected
    _make(org_b, "lead", "L1", {"name": "Pat"})
    assert "lead" in _kinds(org_b, owner_a)


def test_preferences_require_a_session_and_membership_of_that_organization():
    client, org = _org()
    other_client, other_org = _org()
    anon = TestClient(app)
    assert anon.get(_url(org, "/preferences")).status_code == 401
    assert anon.put(_url(org, "/preferences/leads"), json={"enabled": False}).status_code == 401
    # another tenant's user cannot read or change this organization's preferences
    assert other_client.get(_url(org, "/preferences")).status_code in (403, 404)
    assert other_client.put(_url(org, "/preferences/leads"), json={"enabled": False}).status_code in (403, 404)
    assert _prefs(client, org)["leads"]["enabled"] is True
    db = SessionLocal()
    try:
        assert db.query(NotificationPreference).filter_by(organization_id=uuid.UUID(org)).count() == 0
        assert other_org
    finally:
        db.close()


# ------------------------------------------------------------------ validation
@pytest.mark.parametrize("bad", ["", "CALLS", "all", "unknown", "calls,leads", "calls;drop table notifications", "%27", "..", "calls%0A"])
def test_an_unknown_category_is_rejected_and_changes_nothing(bad):
    client, org = _org()
    assert client.put(_url(org, f"/preferences/{bad}"), json={"enabled": False}).status_code in (404, 405, 422)
    assert all(p["enabled"] for p in _prefs(client, org).values())


@pytest.mark.parametrize("body", [{}, {"enabled": None}, {"enabled": "maybe"}, {"enabled": [1]}, {"enabled": {"a": 1}}, [], None])
def test_a_bad_body_is_rejected_with_422_and_changes_nothing(body):
    client, org = _org()
    assert client.put(_url(org, "/preferences/leads"), json=body).status_code == 422
    assert _prefs(client, org)["leads"]["enabled"] is True


def test_extra_fields_cannot_name_another_user_or_organization():
    client, org = _org()
    admin_client, admin = _add_user(org, OrgRole.admin)
    r = client.put(_url(org, "/preferences/leads"), json={"enabled": False, "user_id": str(admin), "organization_id": str(uuid.uuid4())})
    assert r.status_code == 200
    assert _prefs(admin_client, org)["leads"]["enabled"] is True and _prefs(client, org)["leads"]["enabled"] is False


# ------------------------------------------------------------------ idempotency, concurrency, constraints
def test_repeating_the_same_choice_keeps_one_row():
    client, org = _org()
    for _ in range(3):
        assert _set(client, org, "leads", False).status_code == 200
    db = SessionLocal()
    try:
        assert db.query(NotificationPreference).filter_by(organization_id=uuid.UUID(org), category="leads").count() == 1
    finally:
        db.close()


def test_concurrent_changes_never_error_and_leave_one_row():
    client, org = _org()
    results = []

    def flip(value):
        c = TestClientFor(org)
        results.append(c.put(_url(org, "/preferences/leads"), json={"enabled": value}).status_code)

    threads = [threading.Thread(target=flip, args=(i % 2 == 0,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == [200] * 8
    db = SessionLocal()
    try:
        assert db.query(NotificationPreference).filter_by(organization_id=uuid.UUID(org), category="leads").count() == 1
    finally:
        db.close()


def test_the_database_refuses_an_unknown_category_and_a_duplicate_choice():
    _client, org = _org()
    owner = _owner_id(org)
    db = SessionLocal()
    try:
        db.add(NotificationPreference(organization_id=uuid.UUID(org), user_id=owner, category="newsletter", enabled=False))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        db.add_all([NotificationPreference(organization_id=uuid.UUID(org), user_id=owner, category="leads", enabled=False) for _ in range(2)])
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()


def test_deleting_the_organization_removes_its_preferences():
    client, org = _org()
    _set(client, org, "leads", False)
    db = SessionLocal()
    try:
        db.query(Organization).filter_by(id=uuid.UUID(org)).delete()
        db.commit()
        assert db.query(NotificationPreference).filter_by(organization_id=uuid.UUID(org)).count() == 0
        assert db.query(Notification).filter_by(organization_id=uuid.UUID(org)).count() == 0
    finally:
        db.close()


def test_a_failure_while_applying_preferences_cannot_break_the_caller(monkeypatch):
    client, org = _org()

    def boom(*a, **k):
        raise RuntimeError("preference lookup broke")

    monkeypatch.setattr(svc, "exists", boom)
    db = SessionLocal()
    try:
        assert svc.create(db, uuid.UUID(org), "lead", "boom", {"name": "Pat"}) is False  # logged, contained: the caller's transaction lives
        db.execute(__import__("sqlalchemy").text("select 1"))
    finally:
        db.close()
    assert _rows(org) == []
    assert client.get(_url(org, "/preferences")).status_code == 200


def test_there_is_no_email_preference_anywhere():
    import inspect

    from app.api.v1 import notifications as api

    assert not [n for n in dir(api) + dir(svc) if "email" in n.lower() and "preference" in n.lower()]
    assert "email" not in " ".join(inspect.signature(svc.set_preference).parameters)


def test_muting_one_category_leaves_every_other_category_flowing():
    client, org = _org()
    owner = _owner_id(org)
    _set(client, org, "leads", False)
    _make(org, "lead", "A", {"name": "Pat"})  # leads: muted
    _make(org, "plan_changed", "B", {"plan_id": "starter"})  # billing: still flows
    _make(org, "low_credits", "C", {"balance_minutes": 3, "threshold_minutes": 10})  # credits: still flows
    assert _kinds(org, owner) == ["plan_changed", "low_credits"]
