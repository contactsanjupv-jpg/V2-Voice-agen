"""Customers must never see provider internals, raw bodies, keys, stack traces or our costs."""
import re
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.llm_extraction import LLMExtractionError
from app.main import app
from app.providers.agent.retell_agent_provider import RetellAgentProvider
from app.providers.retell_client import RetellAPIError
from tests.fakes import FakePhone
from tests.test_agent_config import FakeRetell, _body, _new_org, _put, _seed_business

LEAKS = re.compile(r"retell|paddle|groq|anthropic|sk-[a-z0-9]|api[_ ]?key|traceback|secret|internal|status=\d{3}|\b4\d\d\b|\b5\d\d\b", re.I)


def _assert_clean(text: str):
    assert not LEAKS.search(text), f"provider/internal detail leaked: {text!r}"


def _html_fetch(monkeypatch):
    monkeypatch.setattr(
        "app.services.website_import_service.safe_fetch",
        lambda url: SimpleNamespace(text="<html><title>Maple</title><body>hello</body></html>", final_url=url),
    )


def test_llm_failure_during_import_shows_a_safe_message(monkeypatch):
    _html_fetch(monkeypatch)

    def boom(*a, **k):
        raise LLMExtractionError("Groq extraction call failed: 401 {\"error\":\"invalid api key sk-abc123\"}")

    monkeypatch.setattr("app.services.website_import_service.extract_business_info", boom)
    c, org = _new_org()
    resp = c.post(f"/api/v1/orgs/{org}/businesses/import-website", json={"url": "https://maple.example"})
    assert resp.status_code == 422
    _assert_clean(resp.text)
    assert "enter your details manually" in resp.json()["detail"]


def test_network_error_during_extraction_is_also_safe(monkeypatch):
    _html_fetch(monkeypatch)
    monkeypatch.setattr("app.services.website_import_service.extract_business_info", lambda *a, **k: (_ for _ in ()).throw(httpx.ReadTimeout("api.groq.com timed out")))
    c, org = _new_org()
    resp = c.post(f"/api/v1/orgs/{org}/businesses/import-website", json={"url": "https://maple.example"})
    assert resp.status_code == 422
    _assert_clean(resp.text)


def test_blocked_addresses_do_not_explain_why():
    c, org = _new_org()
    resp = c.post(f"/api/v1/orgs/{org}/businesses/import-website", json={"url": "http://127.0.0.1/"})
    assert resp.status_code == 422
    assert "127.0.0.1" not in resp.text and "private" not in resp.text.lower() and "resolved" not in resp.text.lower()


def test_agent_save_failure_hides_the_provider_response(monkeypatch):
    class Leaky(FakeRetell):
        def request(self, method, path, json=None, params=None):
            raise RetellAPIError(500, "Retell exploded: key_live_SECRET123", {"detail": "key_live_SECRET123"})

    leaky = Leaky()
    monkeypatch.setattr("app.services.agent_service.RetellAgentProvider", lambda: RetellAgentProvider(client=leaky))
    c, org = _new_org()
    biz, voice = _seed_business(org)
    resp = _put(c, org, biz, _body(voice))
    assert resp.status_code == 502
    assert "SECRET123" not in resp.text
    assert "retell" not in resp.json()["detail"].lower() and "key" not in resp.json()["detail"].lower()


def test_phone_failures_hide_the_provider(monkeypatch):
    from tests.test_phone_lifecycle import _buy, _org

    fake = FakePhone()
    for target in ("app.services.phone_provisioning.RetellPhoneProvider", "app.api.v1.phone_numbers.RetellPhoneProvider"):
        monkeypatch.setattr(target, lambda: fake)
    c, org = _org()
    fake.reject_purchases = 1
    resp = _buy(c, org)
    assert resp.status_code == 502
    _assert_clean(resp.text)
    fake.timeout_after_success = 1
    pending = _buy(c, org)
    assert pending.status_code == 409
    _assert_clean(pending.text)


def test_unexpected_errors_return_a_generic_body_with_no_trace(monkeypatch):
    def explode(*a, **k):
        raise RuntimeError("db password=hunter2 host=10.0.0.5")

    monkeypatch.setattr("app.api.v1.onboarding.derive_onboarding", explode)
    c, org = _new_org()
    tc = TestClient(app, raise_server_exceptions=False)
    tc.cookies.update(c.cookies)
    resp = tc.get(f"/api/v1/orgs/{org}/onboarding")
    assert resp.status_code == 500
    assert "hunter2" not in resp.text and "10.0.0.5" not in resp.text and "Traceback" not in resp.text


def test_customer_api_responses_never_carry_provider_cost_fields():
    c, org = _new_org()
    for path in ("calls", "leads", "usage", "phone-numbers", "billing/subscription"):
        body = c.get(f"/api/v1/orgs/{org}/{path}").text.lower()
        assert "cost" not in body, path
