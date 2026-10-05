"""A fresh database must reach the Voice step with voices available — no manual DB fixing."""
import pytest
from sqlalchemy import text

from app.db.base import SessionLocal
from app.db.models.voice_agent import Voice
from app.providers.retell_client import RetellAPIError
from app.providers.voice.retell_voice_provider import RetellVoiceProvider
from tests.test_tenant_isolation import _signup


class FakeVoiceClient:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def request(self, method, path, json=None, params=None):
        self.calls += 1
        if self.fail:
            raise RetellAPIError(500, "provider internals: secret-body", {"x": "y"})
        return [
            {"voice_id": f"v{i}", "voice_name": f"Voice {i}", "provider": "elevenlabs", "gender": "female",
             "accent": "American", "age": "Young", "preview_audio_url": "https://x.example/a.mp3"}
            for i in range(3)
        ]


def _empty_catalog():
    db = SessionLocal()
    db.execute(text("TRUNCATE voices CASCADE"))  # a brand-new database
    db.commit()
    db.close()


def _user(tag):
    return _signup(f"voice-{tag}@voice-test.com", f"Voice Org {tag}")


def test_fresh_database_gets_voices_on_first_request_and_syncs_only_once(monkeypatch):
    _empty_catalog()
    client = FakeVoiceClient()
    monkeypatch.setattr("app.workers.voice_catalog_sync.RetellVoiceProvider", lambda: RetellVoiceProvider(client=client))
    c, _org = _user("a1")
    first = c.get("/api/v1/voices")
    assert first.status_code == 200 and len(first.json()) == 3
    assert c.get("/api/v1/voices").status_code == 200
    assert client.calls == 1  # the second request read our own catalog
    assert {"id", "name", "preview_url"} <= set(first.json()[0])


def test_provider_failure_gives_a_safe_503_and_recovers_next_time(monkeypatch):
    _empty_catalog()
    bad = FakeVoiceClient(fail=True)
    monkeypatch.setattr("app.workers.voice_catalog_sync.RetellVoiceProvider", lambda: RetellVoiceProvider(client=bad))
    c, _org = _user("b1")
    resp = c.get("/api/v1/voices")
    assert resp.status_code == 503
    body = resp.text.lower()
    assert "secret-body" not in body and "retell" not in body and "provider internals" not in body

    good = FakeVoiceClient()
    monkeypatch.setattr("app.workers.voice_catalog_sync.RetellVoiceProvider", lambda: RetellVoiceProvider(client=good))
    assert len(c.get("/api/v1/voices").json()) == 3


def test_an_existing_catalog_is_never_resynced_on_request(monkeypatch):
    _empty_catalog()
    client = FakeVoiceClient()
    monkeypatch.setattr("app.workers.voice_catalog_sync.RetellVoiceProvider", lambda: RetellVoiceProvider(client=client))
    c, _org = _user("c1")
    c.get("/api/v1/voices")
    c.get("/api/v1/voices?gender=male")  # filtered to nothing, but the catalog is not empty
    assert client.calls == 1
