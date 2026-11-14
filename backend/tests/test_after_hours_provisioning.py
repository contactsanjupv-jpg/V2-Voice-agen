"""
After-hours twin provisioning: the closed receptionist must actually get created, and the owner must be able to tell
when it hasn't been. Regression tests for: schedule saved while the server flag was off, then an identical re-save
(used to be a no-op that never created the twin); status that ignored a missing twin; a Retell failure part-way
through creating the twin (retry must resume, not duplicate); and the closed test call's 409 not saying why.
All against a recorder standing in for Retell; no real call is made.
"""

import pytest

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.voice_agent import Agent
from tests.test_advanced_settings import _patch, _profile, _setup
from tests.test_agent_config import _agent_id, _agent_row, fake  # noqa: F401
from tests.test_inbound_call_resolution import _retell_key  # noqa: F401  (autouse)

settings = get_settings()
SCHED = {
    "enabled": True,
    "weekly": {"mon": [["09:00", "17:00"]], "tue": [["09:00", "17:00"]]},
}
PROFILE = {
    "name": "Sunrise Dental",
    "timezone": "America/Chicago",
    "hours_schedule": SCHED,
}


@pytest.fixture(autouse=True)
def _limits(monkeypatch):
    monkeypatch.setattr(settings, "AFTER_HOURS_ROUTING_ENABLED", True)
    monkeypatch.setattr(settings, "FREE_TEST_CALLS_PER_ORG", 5)
    monkeypatch.setattr(settings, "TEST_CALLS_PER_DAY_PER_ORG", 1000)


def _flag(monkeypatch, on):
    monkeypatch.setattr(settings, "AFTER_HOURS_ROUTING_ENABLED", on)


def _cfg(c, org, biz):
    return c.get(f"/api/v1/orgs/{org}/agents/{biz}/config").json()


def _handling(c, org, biz):
    return c.get(f"/api/v1/orgs/{org}/agents/{biz}/call-handling").json()


def _listed(c, org):
    return c.get(f"/api/v1/orgs/{org}/agents").json()[0]


def _hours(c, org, biz):
    h = _profile(c, org, biz).json()["hours_schedule"]
    return {"enabled": h["enabled"], "weekly": h["weekly"]}


def _retell_calls(fake):  # noqa: F811
    return len(fake.calls)


# ---------------- the no-op re-save bug ----------------


def test_a_schedule_saved_while_the_flag_was_off_gets_its_twin_when_the_same_hours_are_saved_again(
    fake, monkeypatch
):  # noqa: F811
    c, org, biz, _v = _setup(fake)
    _flag(monkeypatch, False)
    assert _patch(c, org, biz, PROFILE).status_code == 200
    assert _agent_row(biz).closed_retell_agent_id is None  # skipped: the flag was off
    llms_before = len(fake.to("/create-retell-llm"))

    _flag(monkeypatch, True)
    assert (
        _patch(c, org, biz, PROFILE).status_code == 200
    )  # IDENTICAL payload: nothing "changed"

    row = _agent_row(biz)
    assert row.closed_retell_agent_id and row.closed_retell_llm_id
    assert (
        len(fake.to("/create-retell-llm")) == llms_before + 1
        and len(fake.to("/create-agent")) == 2
    )  # one twin, no dupes
    assert _hours(c, org, biz) == SCHED  # nothing about the schedule was rewritten


def test_once_the_twin_exists_an_identical_save_does_nothing_at_the_provider(fake):  # noqa: F811
    c, org, biz, _v = _setup(fake)
    assert _patch(c, org, biz, PROFILE).status_code == 200
    assert _agent_row(biz).closed_retell_agent_id
    version, calls = _agent_row(biz).version, _retell_calls(fake)
    for _ in range(2):
        assert _patch(c, org, biz, PROFILE).status_code == 200
    assert (
        _retell_calls(fake) == calls and _agent_row(biz).version == version
    )  # no needless sync, no version churn


def test_a_flag_off_identical_save_still_does_nothing(fake, monkeypatch):  # noqa: F811
    c, org, biz, _v = _setup(fake)
    _flag(monkeypatch, False)
    assert _patch(c, org, biz, PROFILE).status_code == 200
    calls = _retell_calls(fake)
    assert _patch(c, org, biz, PROFILE).status_code == 200
    assert (
        _retell_calls(fake) == calls and _agent_row(biz).closed_retell_agent_id is None
    )


def test_a_business_without_a_schedule_is_never_synced_by_an_unchanged_save(fake):  # noqa: F811
    c, org, biz, _v = _setup(fake)
    assert _patch(c, org, biz, {"name": "Sunrise Dental"}).status_code == 200
    calls = _retell_calls(fake)
    assert _patch(c, org, biz, {"name": "Sunrise Dental"}).status_code == 200
    assert (
        _retell_calls(fake) == calls and _agent_row(biz).closed_retell_agent_id is None
    )


def test_an_agent_that_was_never_synced_is_not_provisioned_by_a_profile_save(
    fake, monkeypatch
):  # noqa: F811
    c, org, biz, _v = _setup(fake)
    db = SessionLocal()
    try:
        a = db.query(Agent).filter(Agent.business_id == biz).one()
        a.retell_agent_id = a.retell_llm_id = None
        db.commit()
    finally:
        db.close()
    calls = _retell_calls(fake)
    assert _patch(c, org, biz, PROFILE).status_code == 200
    assert (
        _retell_calls(fake) == calls
    )  # existing rule kept: the receptionist is created by its own save, not here


# ---------------- status: a missing twin is not "synced" ----------------


def test_the_owner_is_not_told_everything_is_applied_while_the_twin_is_missing(
    fake, monkeypatch
):  # noqa: F811
    c, org, biz, _v = _setup(fake)
    _flag(monkeypatch, False)
    assert _patch(c, org, biz, PROFILE).status_code == 200
    # flag off: nothing is owed, so the receptionist is honestly up to date
    assert (
        _cfg(c, org, biz)["synced"] is True
        and _handling(c, org, biz)["sync_state"] == "synced"
        and _listed(c, org)["synced"] is True
    )

    _flag(monkeypatch, True)  # routing is now expected, but the twin doesn't exist
    assert _cfg(c, org, biz)["synced"] is False
    assert _handling(c, org, biz)["sync_state"] == "pending"
    assert _listed(c, org)["synced"] is False

    assert (
        _patch(c, org, biz, PROFILE).status_code == 200
    )  # the identical save provisions it
    assert (
        _cfg(c, org, biz)["synced"] is True
        and _handling(c, org, biz)["sync_state"] == "synced"
        and _listed(c, org)["synced"] is True
    )


def test_the_normal_receptionist_keeps_working_while_the_twin_is_missing(
    fake, monkeypatch
):  # noqa: F811
    c, org, biz, _v = _setup(fake)
    _flag(monkeypatch, False)
    assert _patch(c, org, biz, PROFILE).status_code == 200
    _flag(monkeypatch, True)
    row = _agent_row(biz)
    r = c.post(
        f"/api/v1/orgs/{org}/agents/{_agent_id(c, org)}/test-call"
    )  # default variant: normal agent, unchanged gate
    assert r.status_code == 200, r.text
    assert fake.to("/v3/create-web-call")[-1][2]["agent_id"] == row.retell_agent_id


# ---------------- partial Retell failure: retry resumes ----------------


def test_a_failure_after_the_twin_llm_is_created_resumes_on_retry_without_duplicates(
    fake,
):  # noqa: F811
    c, org, biz, _v = _setup(fake)
    fake.fail["/create-agent"] = (
        1  # the normal agent already exists, so this hits the twin's create-agent
    )
    r = _patch(c, org, biz, PROFILE)
    assert (
        r.status_code == 502 and "saved" in r.json()["detail"].lower()
    )  # saved, not applied: never a silent success
    row = _agent_row(biz)
    assert (
        row.closed_retell_llm_id and row.closed_retell_agent_id is None
    )  # the partial state the bug report worried about
    assert _hours(c, org, biz) == SCHED  # the owner's hours were kept
    assert (
        _cfg(c, org, biz)["synced"] is False
    )  # and the owner is told it isn't applied
    llm_id, llm_creates = row.closed_retell_llm_id, len(fake.to("/create-retell-llm"))

    assert (
        _patch(c, org, biz, PROFILE).status_code == 200
    )  # retry with an identical save
    row = _agent_row(biz)
    assert row.closed_retell_llm_id == llm_id  # resumed from the persisted LLM
    assert len(fake.to("/create-retell-llm")) == llm_creates  # no second twin LLM
    assert fake.to(
        f"/update-retell-llm/{llm_id}"
    )  # it updated the existing one instead
    assert (
        row.closed_retell_agent_id and len(fake.to("/create-agent")) == 3
    )  # normal + failed twin attempt + successful retry
    assert _cfg(c, org, biz)["synced"] is True


def test_a_failure_creating_the_twin_llm_leaves_nothing_half_made_and_retries_cleanly(
    fake,
):  # noqa: F811
    c, org, biz, _v = _setup(fake)
    llms = len(fake.to("/create-retell-llm"))
    fake.fail["/create-retell-llm"] = 1
    assert _patch(c, org, biz, PROFILE).status_code == 502
    row = _agent_row(biz)
    assert row.closed_retell_llm_id is None and row.closed_retell_agent_id is None
    assert _patch(c, org, biz, PROFILE).status_code == 200
    row = _agent_row(biz)
    assert (
        row.closed_retell_llm_id
        and row.closed_retell_agent_id
        and len(fake.to("/create-retell-llm")) == llms + 2
    )


# ---------------- the closed test call explains itself ----------------


def _closed_409(c, org):
    r = c.post(
        f"/api/v1/orgs/{org}/agents/{_agent_id(c, org)}/test-call?variant=closed"
    )
    assert r.status_code == 409
    return r.json()["detail"]


def test_the_closed_test_call_409_says_the_server_flag_is_off(fake, monkeypatch):  # noqa: F811
    c, org, biz, _v = _setup(fake)
    _flag(monkeypatch, False)
    assert _patch(c, org, biz, PROFILE).status_code == 200
    msg = _closed_409(c, org)
    assert "AFTER_HOURS_ROUTING_ENABLED" in msg and "restart" in msg.lower()


def test_the_closed_test_call_409_says_to_turn_on_opening_hours(fake):  # noqa: F811
    c, org, biz, _v = _setup(fake)
    assert "opening hours" in _closed_409(c, org)


def test_the_closed_test_call_409_says_the_twin_has_not_been_created_yet(
    fake, monkeypatch
):  # noqa: F811
    c, org, biz, _v = _setup(fake)
    _flag(monkeypatch, False)
    assert _patch(c, org, biz, PROFILE).status_code == 200
    _flag(monkeypatch, True)
    msg = _closed_409(c, org)
    assert "hasn't been created yet" in msg and "AFTER_HOURS_ROUTING_ENABLED" not in msg


# ---------------- the UI can tell the server switch is off ----------------


def test_the_profile_reports_whether_the_server_will_use_the_saved_hours(
    fake, monkeypatch
):  # noqa: F811
    c, org, biz, _v = _setup(fake)
    assert _profile(c, org, biz).json()["after_hours_routing_available"] is True
    _flag(monkeypatch, False)
    assert _profile(c, org, biz).json()["after_hours_routing_available"] is False
