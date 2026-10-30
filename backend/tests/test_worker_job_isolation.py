"""
Worker pass isolation. Every job in run_worker.run_once must run even when an EARLIER job fails, above all
enforce_entitlements: it is what stops unbilled usage (lapsed subscriptions, exhausted credit) and, if it is skipped,
a customer who should be paused keeps getting service at our cost. A failure must still be reported (failed_jobs) so
monitoring sees it.
"""
import pytest

from app.workers import run_worker
from tests.fakes import FakePhone


def _boom(*a, **k):
    raise ValueError("unexpected provider/database failure")


@pytest.fixture
def ran(monkeypatch):
    log: list[str] = []
    monkeypatch.setattr(run_worker, "retry_due_events", lambda: log.append("events") or 0)
    monkeypatch.setattr(run_worker, "reconcile_provisioning", lambda db, provider: log.append("provisioning") or 0)
    monkeypatch.setattr(run_worker, "enforce_entitlements", lambda db, provider: log.append("entitlements") or {})
    monkeypatch.setattr(run_worker, "settle_auto_provisioning", lambda db, provider: log.append("auto") or 0)
    monkeypatch.setattr(run_worker, "reconcile_topups", lambda db: log.append("topups") or 0)
    return log


def test_all_jobs_run_when_nothing_fails(ran):
    summary = run_worker.run_once(phone_provider=FakePhone())
    assert ran == ["events", "provisioning", "entitlements", "auto", "topups"]
    assert summary["failed_jobs"] == []


def test_a_failing_provisioning_sweep_does_not_skip_entitlement_enforcement(ran, monkeypatch):
    monkeypatch.setattr(run_worker, "reconcile_provisioning", _boom)
    summary = run_worker.run_once(phone_provider=FakePhone())
    assert ran == ["events", "entitlements", "auto", "topups"]
    assert summary["failed_jobs"] == ["provisioning"]


def test_a_failing_entitlement_enforcement_does_not_skip_the_jobs_after_it(ran, monkeypatch):
    monkeypatch.setattr(run_worker, "enforce_entitlements", _boom)
    summary = run_worker.run_once(phone_provider=FakePhone())
    assert ran == ["events", "provisioning", "auto", "topups"]
    assert summary["failed_jobs"] == ["entitlements"]


def test_a_failing_topup_sweep_is_reported_and_does_not_break_the_pass(ran, monkeypatch):
    monkeypatch.setattr(run_worker, "reconcile_topups", _boom)
    summary = run_worker.run_once(phone_provider=FakePhone())
    assert ran == ["events", "provisioning", "entitlements", "auto"]
    assert summary["failed_jobs"] == ["topups"]


def test_several_failures_are_all_reported_and_the_rest_still_run(ran, monkeypatch):
    monkeypatch.setattr(run_worker, "reconcile_provisioning", _boom)
    monkeypatch.setattr(run_worker, "settle_auto_provisioning", _boom)
    summary = run_worker.run_once(phone_provider=FakePhone())
    assert ran == ["events", "entitlements", "topups"]
    assert summary["failed_jobs"] == ["provisioning", "auto_provisioning"]


def test_paddle_not_configured_is_not_a_failure_and_skips_only_topups(ran, monkeypatch):
    def not_configured(db):
        raise RuntimeError("Paddle is not configured")

    monkeypatch.setattr(run_worker, "reconcile_topups", not_configured)
    summary = run_worker.run_once(phone_provider=FakePhone())
    assert ran == ["events", "provisioning", "entitlements", "auto"]
    assert summary["failed_jobs"] == []
    assert "topups" not in summary
