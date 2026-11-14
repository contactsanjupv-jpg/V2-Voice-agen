"""Level 8C through the real HTTP API: saving hours, twin creation on save, and trying the twin in the browser."""
import pytest

from app.config import get_settings
from tests.test_advanced_settings import _patch, _profile, _setup
from tests.test_agent_config import _agent_row, fake  # noqa: F401
from tests.test_inbound_call_resolution import _retell_key  # noqa: F401  (autouse)

settings = get_settings()
SCHED = {"enabled": True, "weekly": {"mon": [["09:00", "17:00"]], "tue": [["09:00", "17:00"]]}, "exceptions": {"2026-12-25": []}}


@pytest.fixture(autouse=True)
def _flag(monkeypatch):
    monkeypatch.setattr(settings, "AFTER_HOURS_ROUTING_ENABLED", True)
    monkeypatch.setattr(settings, "FREE_TEST_CALLS_PER_ORG", 5)
    monkeypatch.setattr(settings, "TEST_CALLS_PER_DAY_PER_ORG", 1000)


def test_the_schedule_saves_reloads_and_is_absent_until_set(fake):  # noqa: F811
    c, org, biz, _v = _setup(fake)
    assert _profile(c, org, biz).json()["hours_schedule"] is None
    r = _patch(c, org, biz, {"name": "Sunrise Dental", "timezone": "America/Chicago", "hours_schedule": SCHED})
    assert r.status_code == 200 and r.json()["hours_schedule"] == SCHED
    assert _profile(c, org, biz).json()["hours_schedule"] == SCHED


@pytest.mark.parametrize("bad", [
    {"enabled": True, "weekly": {"mon": [["09:00", "10:00"], ["09:30", "11:00"]]}},
    {"enabled": True, "weekly": {"noday": [["09:00", "10:00"]]}},
    {"enabled": True, "weekly": {}},
    "always open",
])
def test_an_invalid_schedule_is_rejected_and_nothing_changes(fake, bad):  # noqa: F811
    c, org, biz, _v = _setup(fake)
    v = _agent_row(biz).version
    assert _patch(c, org, biz, {"name": "X", "hours_schedule": bad}).status_code == 422
    assert _profile(c, org, biz).json()["hours_schedule"] is None and _agent_row(biz).version == v


def test_omitting_the_schedule_keeps_it_and_disabling_it_turns_routing_off(fake):  # noqa: F811
    c, org, biz, _v = _setup(fake)
    _patch(c, org, biz, {"name": "X", "timezone": "America/Chicago", "hours_schedule": SCHED})
    _patch(c, org, biz, {"name": "X renamed"})
    assert _profile(c, org, biz).json()["hours_schedule"] == SCHED
    _patch(c, org, biz, {"name": "X renamed", "hours_schedule": {**SCHED, "enabled": False}})
    assert _profile(c, org, biz).json()["hours_schedule"]["enabled"] is False


def test_saving_a_schedule_creates_the_twin_without_a_transfer_tool(fake):  # noqa: F811
    c, org, biz, _v = _setup(fake)
    assert not fake.to("/create-retell-llm")[1:], "only the normal receptionist exists before opting in"
    n0 = len(fake.to("/create-retell-llm"))
    r = _patch(c, org, biz, {"name": "X", "timezone": "America/Chicago", "hours_schedule": SCHED})
    assert r.status_code == 200
    row = _agent_row(biz)
    assert row.closed_retell_agent_id and row.closed_retell_llm_id and row.synced_version == row.version
    created = fake.to("/create-retell-llm")[n0:]
    assert len(created) == 1 and "AFTER HOURS" in created[0][2]["general_prompt"]
    assert not any(t.get("type") == "transfer_call" for t in created[0][2].get("general_tools") or [])


def test_a_business_that_never_opts_in_never_gets_a_twin(fake):  # noqa: F811
    c, org, biz, _v = _setup(fake)
    _patch(c, org, biz, {"name": "Renamed", "timezone": "America/Chicago"})
    assert _agent_row(biz).closed_retell_agent_id is None


def test_trying_the_after_hours_receptionist_in_the_browser(fake):  # noqa: F811
    from tests.test_retell_v3_web_call import _agent_id
    c, org, biz, _v = _setup(fake)
    url = f"/api/v1/orgs/{org}/agents/{_agent_id(c, org)}/test-call"
    assert c.post(url + "?variant=closed").status_code == 409  # no twin yet
    _patch(c, org, biz, {"name": "X", "timezone": "America/Chicago", "hours_schedule": SCHED})
    n0 = len(fake.to("/v3/create-web-call"))
    r = c.post(url + "?variant=closed")
    assert r.status_code == 200, r.text
    body = fake.to("/v3/create-web-call")[n0][2]
    assert body["agent_id"] == _agent_row(biz).closed_retell_agent_id
    r2 = c.post(url)  # default variant still the normal agent
    assert r2.status_code == 200 and fake.to("/v3/create-web-call")[n0 + 1][2]["agent_id"] == _agent_row(biz).retell_agent_id
    assert c.post(url + "?variant=bogus").status_code == 422


@pytest.mark.parametrize("query,which", [("", "normal"), ("?variant=normal", "normal"), ("?variant=closed", "closed")])
def test_the_test_call_connects_to_the_selected_receptionist(fake, query, which):  # noqa: F811
    """The browser test call must start the Retell agent that the variant names, and return that call's details."""
    from tests.test_retell_v3_web_call import _agent_id

    c, org, biz, _v = _setup(fake)
    assert _patch(c, org, biz, {"name": "X", "timezone": "America/Chicago", "hours_schedule": SCHED}).status_code == 200
    row = _agent_row(biz)
    assert row.closed_retell_agent_id and row.closed_retell_agent_id != row.retell_agent_id
    n = len(fake.to("/v3/create-web-call"))
    r = c.post(f"/api/v1/orgs/{org}/agents/{_agent_id(c, org)}/test-call{query}")
    assert r.status_code == 200, r.text
    sent = fake.to("/v3/create-web-call")[n][2]
    assert sent["agent_id"] == (row.closed_retell_agent_id if which == "closed" else row.retell_agent_id)
    assert r.json()["call_id"] and r.json()["access_token"] and len(fake.to("/v3/create-web-call")) == n + 1
