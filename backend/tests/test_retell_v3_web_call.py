"""
Retell web calls use POST /v3/create-web-call only (v2 is retired: HTTP 400 "deprecated API"). The v3 response
(call_id, access_token, transport, ice_servers, expires_at) is validated and every connection field is handed to the
browser, because the SDK needs the call_id and transport as well as the token. Atla's auth, ownership, rate limit,
usage classification and webhook correlation are unchanged.
"""
import json
import uuid

import pytest

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.billing import UsageLedgerEntry
from app.db.models.calls import Call
from app.providers.call.retell_call_provider import CREATE_WEB_CALL_PATH, RetellCallProvider, parse_web_call
from app.providers.retell_client import RetellAPIError
from tests.billing_helpers import new_org, subscribe
from tests.test_agent_config import _agent_id, _body, _put, _seed_business, fake  # noqa: F401
from tests.test_call_pipeline import _call, _post as retell_post

settings = get_settings()


@pytest.fixture(autouse=True)
def _retell_key(monkeypatch):
    monkeypatch.setattr(settings, "RETELL_API_KEY", "test-retell-key")  # signs the simulated webhooks


V3 = {
    "call_id": "call_abc123",
    "access_token": "tok_secret_abc",
    "transport": "gateway",
    "ice_servers": [
        {"urls": ["stun:stun.example.test:3478"]},
        {"urls": "turn:turn.example.test:443", "username": "user1", "credential": "cred1"},
    ],
    "expires_at": 1_800_000_000_000,
}


class Recorder:
    def __init__(self, answer=None, error=None):
        self.sent, self.answer, self.error = [], answer, error

    def request(self, method, path, json=None, params=None):
        self.sent.append((method, path, json))
        if self.error:
            raise self.error
        return self.answer if self.answer is not None else dict(V3)


# ---------------- the request ----------------

def test_the_request_goes_to_v3_only_with_the_agent_and_metadata():
    client = Recorder()
    RetellCallProvider(client=client).create_test_call("agent_1", metadata={"kind": "test", "organization_id": "o"})
    assert CREATE_WEB_CALL_PATH == "/v3/create-web-call"
    assert client.sent == [("POST", "/v3/create-web-call", {"agent_id": "agent_1", "metadata": {"kind": "test", "organization_id": "o"}})]


@pytest.mark.parametrize("status", [400, 401, 404, 422, 429, 500])
def test_a_provider_error_is_final_and_the_deprecated_v2_path_is_never_tried(status):
    client = Recorder(error=RetellAPIError(status, "nope"))
    with pytest.raises(RetellAPIError) as exc:
        RetellCallProvider(client=client).create_test_call("agent_1")
    assert exc.value.status_code == status
    assert [p for _m, p, _b in client.sent] == ["/v3/create-web-call"]  # exactly one request, never /v2


def test_no_v2_path_remains_in_the_backend():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    quoted = ('"/v2/create-web-call"', "'/v2/create-web-call'")  # a docstring may explain the history; code may not use it
    assert not [p for p in root.rglob("*.py") if any(q in p.read_text() for q in quoted)]


# ---------------- the response ----------------

def test_every_v3_connection_field_is_preserved():
    s = RetellCallProvider(client=Recorder()).create_test_call("a")
    assert s.provider_call_id == "call_abc123" and s.access_token == "tok_secret_abc"
    assert s.transport == "gateway" and s.expires_at == 1_800_000_000_000 and s.url is None
    assert s.ice_servers == V3["ice_servers"]


def test_ice_server_optional_fields_are_only_kept_when_present():
    s = parse_web_call({**V3, "ice_servers": [{"urls": "stun:x:1", "username": None}]})
    assert s.ice_servers == [{"urls": "stun:x:1"}]  # a null username would make WebRTC reject the server


def test_missing_ice_servers_degrades_to_the_browsers_defaults_instead_of_failing():
    raw = {k: v for k, v in V3.items() if k != "ice_servers"}
    assert parse_web_call(raw).ice_servers == []


@pytest.mark.parametrize(
    "bad",
    [
        None, [], "x", {},
        {**V3, "call_id": ""}, {**V3, "call_id": 5}, {k: v for k, v in V3.items() if k != "call_id"},
        {**V3, "access_token": ""}, {k: v for k, v in V3.items() if k != "access_token"},
        {k: v for k, v in V3.items() if k != "transport"}, {**V3, "transport": "carrier-pigeon"},
        {**V3, "transport": "livekit"},  # livekit needs a url
        {**V3, "ice_servers": "turn:x"}, {**V3, "ice_servers": ["turn:x"]}, {**V3, "ice_servers": [{"username": "u"}]},
        {**V3, "ice_servers": [{"urls": []}]}, {**V3, "ice_servers": [{"urls": "t:x", "credential": 7}]},
        {**V3, "expires_at": "soon"}, {**V3, "expires_at": True},
    ],
)
def test_an_unusable_response_is_an_ambiguous_provider_error_that_never_leaks_the_body(bad):
    with pytest.raises(RetellAPIError) as exc:
        parse_web_call(bad)
    assert exc.value.ambiguous is True and exc.value.body is None  # a call may exist; the token is not attached
    assert "tok_secret_abc" not in str(exc.value)


def test_livekit_with_a_url_is_accepted():
    s = parse_web_call({**V3, "transport": "livekit", "url": "wss://x.livekit.test"})
    assert s.transport == "livekit" and s.url == "wss://x.livekit.test"


# ---------------- through the Atla API ----------------

def _ready_org():
    c, org = new_org("v3")
    biz, voice = _seed_business(org)
    assert _put(c, org, biz, _body(voice)).status_code == 200
    return c, org, f"/api/v1/orgs/{org}/agents/{_agent_id(c, org)}/test-call"


def test_the_api_returns_every_field_the_browser_needs(fake, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "FREE_TEST_CALLS_PER_ORG", 1)
    c, org, url = _ready_org()
    r = c.post(url)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["call_id"].startswith("call_") and d["access_token"] == "tok_test" and d["transport"] == "gateway"
    assert d["expires_at"] == 1_800_000_000_000 and d["max_seconds"] == settings.TEST_CALL_MAX_SECONDS
    assert d["ice_servers"] == [
        {"urls": ["stun:stun.example.test:3478"]},  # no null username/credential keys
        {"urls": "turn:turn.example.test:443", "username": "u", "credential": "c"},
    ]
    assert "url" not in d  # absent, not null


def test_the_secrets_are_not_persisted_or_logged(fake, monkeypatch, caplog):  # noqa: F811
    monkeypatch.setattr(settings, "FREE_TEST_CALLS_PER_ORG", 1)
    c, org, url = _ready_org()
    with caplog.at_level("DEBUG"):
        assert c.post(url).status_code == 200
    assert "tok_test" not in caplog.text and "turn.example.test" not in caplog.text
    db = SessionLocal()
    try:
        dumped = json.dumps([[str(v) for v in r.__dict__.values()] for r in db.query(Call).filter(Call.organization_id == uuid.UUID(org))])
        assert "tok_test" not in dumped and "turn.example.test" not in dumped
    finally:
        db.close()


def test_a_malformed_provider_answer_is_a_502_and_keeps_the_free_call_spent(fake, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "FREE_TEST_CALLS_PER_ORG", 1)
    c, org, url = _ready_org()
    orig = fake.request

    def bad(method, path, json=None, params=None):
        out = orig(method, path, json, params)
        if path == "/v3/create-web-call":
            out = {k: v for k, v in out.items() if k != "transport"}
        return out

    monkeypatch.setattr(fake, "request", bad)
    r = c.post(url)
    assert r.status_code == 502 and "tok_test" not in r.text
    # a call may exist at Retell: no extra free test, and no leftover in-flight placeholder
    assert c.post(url).status_code == 402
    db = SessionLocal()
    try:
        assert db.query(Call).filter(Call.organization_id == uuid.UUID(org), Call.retell_call_id.like("pending:%")).count() == 0
    finally:
        db.close()


def test_a_v2_deprecation_400_from_retell_is_a_clean_502_not_a_retry(fake, monkeypatch):  # noqa: F811
    c, org, url = _ready_org()
    fake.fail["/v3/create-web-call"] = 0
    orig = fake.request

    def deprecated(method, path, json=None, params=None):
        if path == "/v3/create-web-call":
            fake.calls.append((method, path, json))
            raise RetellAPIError(400, "deprecated API", {"error": "deprecated"})
        return orig(method, path, json, params)

    monkeypatch.setattr(fake, "request", deprecated)
    assert c.post(url).status_code == 502
    assert len(fake.to("/v3/create-web-call")) == 1 and fake.to("/v2/create-web-call") == []


# ---------------- webhook correlation, classification, protections ----------------

def test_the_v3_call_id_correlates_the_webhook_to_the_call_and_is_classified(fake, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "FREE_TEST_CALLS_PER_ORG", 1)
    c, org, url = _ready_org()
    subscribe(org)
    call_id = c.post(url).json()["call_id"]
    db = SessionLocal()
    try:
        row = db.query(Call).filter_by(retell_call_id=call_id).one()  # the placeholder now carries the v3 call_id
        assert str(row.organization_id) == org
    finally:
        db.close()
    retell_post("call_ended", _call(call_id, fake.ids["agent"], None, call_type="web_call", direction=None, from_number=None, duration_ms=60_000, call_cost={"combined_cost": 3.0}))
    db = SessionLocal()
    try:
        u = db.query(UsageLedgerEntry).filter_by(retell_call_id=call_id).one()
        assert u.kind == "test" and float(u.provider_cost) == 3.0 and str(u.organization_id) == org
    finally:
        db.close()


def test_auth_ownership_and_rate_limit_are_unchanged(fake, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "TEST_CALLS_PER_DAY_PER_ORG", 1)
    c, org, url = _ready_org()
    subscribe(org)
    from fastapi.testclient import TestClient

    from app.main import app

    assert TestClient(app).post(url).status_code == 401
    other, _ = new_org("v3o")
    assert other.post(url).status_code == 404
    assert len(fake.to("/v3/create-web-call")) == 0
    assert c.post(url).status_code == 200
    assert c.post(url).status_code == 429
    assert len(fake.to("/v3/create-web-call")) == 1
