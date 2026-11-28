"""Data retention (services/retention.py): off by default, exact cutoffs, tenant-scoped, idempotent, failure-isolated, audited by counts."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, select

from app.config import Settings, get_settings
from app.db.base import SessionLocal
from app.db.models.calls import Call, CallDirection, CallTranscript
from app.db.models.retention import DataRetentionRun
from app.services import retention
from tests.billing_helpers import new_org

NOW = datetime.now(timezone.utc)
SECRET_TEXT = "my card number is 4111 1111 1111 1111 and my name is Jane Q Customer"


@pytest.fixture(autouse=True)
def _policy(monkeypatch):
    cfg = get_settings()
    monkeypatch.setattr(cfg, "TRANSCRIPT_RETENTION_DAYS", 30)
    monkeypatch.setattr(cfg, "CALLER_DATA_RETENTION_DAYS", 0)
    monkeypatch.setattr(cfg, "RETENTION_BATCH_SIZE", 500)
    monkeypatch.setattr(retention, "_last_pass", None)


def _call(org, age_days, *, transcript=True, summary=True, rec=True, caller="+15551230000") -> uuid.UUID:
    db = SessionLocal()
    try:
        c = Call(organization_id=org, retell_call_id=f"ret-{uuid.uuid4().hex}", direction=CallDirection.inbound, caller_number=caller,
                 started_at=NOW - timedelta(days=age_days), duration_seconds=60, status="ended", cost_cents=12,
                 summary=SECRET_TEXT if summary else None, recording_storage_key="rec/key" if rec else None)
        db.add(c)
        db.flush()
        if transcript:
            db.add(CallTranscript(call_id=c.id, transcript={"text": SECRET_TEXT, "segments": None}))
        db.commit()
        return c.id
    finally:
        db.close()


def _state(call_id):
    db = SessionLocal()
    try:
        c = db.get(Call, call_id)
        has_t = db.execute(select(func.count()).select_from(CallTranscript).where(CallTranscript.call_id == call_id)).scalar_one() > 0
        return c.summary, c.recording_storage_key, has_t, c.caller_number, c.duration_seconds, c.cost_cents, c.status
    finally:
        db.close()


def _run():
    db = SessionLocal()
    try:
        return retention.prune(db, NOW)
    finally:
        db.close()


def _audit(org):
    db = SessionLocal()
    try:
        return [(r.kind, r.rows_affected, r.outcome) for r in db.execute(select(DataRetentionRun).where(DataRetentionRun.organization_id == org).order_by(DataRetentionRun.created_at)).scalars()]
    finally:
        db.close()


def _org():
    return uuid.UUID(str(new_org("ret")[1]))


def test_disabled_by_default_and_prunes_nothing(monkeypatch):
    assert Settings.model_fields["TRANSCRIPT_RETENTION_DAYS"].default == 0
    assert Settings.model_fields["CALLER_DATA_RETENTION_DAYS"].default == 0
    assert Settings.model_fields["RETELL_DATA_RETENTION_DAYS"].default is None
    monkeypatch.setattr(get_settings(), "TRANSCRIPT_RETENTION_DAYS", 0)
    org = _org()
    cid = _call(org, 4000)
    db = SessionLocal()
    try:
        assert retention.prune_pass(db) is None
    finally:
        db.close()
    assert _state(cid)[0] == SECRET_TEXT and _state(cid)[2] is True


def test_old_content_is_removed_recent_content_is_kept_and_the_call_record_survives():
    org = _org()
    old, recent, edge_in, edge_out = _call(org, 31), _call(org, 5), _call(org, 29), _call(org, 31, transcript=False, summary=False, rec=False)
    out = _run()
    assert out["transcripts"] >= 1
    s, rec, has_t, caller, dur, cost, status = _state(old)
    assert (s, rec, has_t) == (None, None, False)
    assert (caller, dur, cost, status) == ("+15551230000", 60, 12, "ended")  # usage/billing facts and the caller are untouched by this policy
    assert _state(recent)[0] == SECRET_TEXT and _state(recent)[2] is True
    assert _state(edge_in)[2] is True  # 29 days < 30
    assert _state(edge_out)[0] is None
    assert _audit(org) == [("transcripts", 1, "ok")]  # only the call that had content counts


def test_pruning_is_idempotent_and_a_second_pass_changes_nothing():
    org = _org()
    _call(org, 100)
    first = _run()
    assert first["transcripts"] >= 1
    before = _audit(org)
    again = _run()
    assert _audit(org) == before and again["failed_orgs"] == 0


def test_every_organization_is_pruned_and_none_touches_another_tenants_recent_data():
    a, b = _org(), _org()
    a_old, b_old, b_recent = _call(a, 90), _call(b, 90), _call(b, 1)
    _run()
    assert _state(a_old)[0] is None and _state(b_old)[0] is None
    assert _state(b_recent)[0] == SECRET_TEXT
    assert [x[1] for x in _audit(a)] == [1] and [x[1] for x in _audit(b)] == [1]


def test_batches_cover_more_calls_than_the_batch_size(monkeypatch):
    monkeypatch.setattr(get_settings(), "RETENTION_BATCH_SIZE", 3)
    org = _org()
    ids = [_call(org, 60) for _ in range(8)]
    _run()
    assert all(_state(i)[0] is None and _state(i)[2] is False for i in ids)
    assert _audit(org) == [("transcripts", 8, "ok")]


def test_one_organization_failing_is_recorded_and_does_not_stop_the_others(monkeypatch):
    bad, good = _org(), _org()
    bad_call, good_call = _call(bad, 60), _call(good, 60)
    real = retention._prune_org

    def flaky(db, org, kind, cutoff, batch):
        if org == bad:
            raise RuntimeError("simulated database error")
        return real(db, org, kind, cutoff, batch)

    monkeypatch.setattr(retention, "_prune_org", flaky)
    out = _run()
    assert out["failed_orgs"] == 1
    assert _state(good_call)[0] is None
    assert _state(bad_call)[0] == SECRET_TEXT  # untouched, still eligible
    assert _audit(bad) == [("transcripts", 0, "failed")]
    monkeypatch.setattr(retention, "_prune_org", real)  # recovery: the next pass finishes the job
    _run()
    assert _state(bad_call)[0] is None and _audit(bad)[-1] == ("transcripts", 1, "ok")


def test_a_failure_in_the_middle_of_a_batch_rolls_that_batch_back(monkeypatch):
    org = _org()
    cid = _call(org, 60)
    db = SessionLocal()
    real_commit = db.commit
    try:
        monkeypatch.setattr(db, "commit", lambda: (_ for _ in ()).throw(RuntimeError("commit failed")))
        with pytest.raises(RuntimeError):
            retention._prune_org(db, org, "transcripts", NOW - timedelta(days=30), 500)
        db.rollback()
        monkeypatch.setattr(db, "commit", real_commit)
    finally:
        db.close()
    assert _state(cid)[0] == SECRET_TEXT and _state(cid)[2] is True  # nothing half-deleted


def test_caller_data_policy_clears_only_the_number_on_old_calls(monkeypatch):
    monkeypatch.setattr(get_settings(), "TRANSCRIPT_RETENTION_DAYS", 0)
    monkeypatch.setattr(get_settings(), "CALLER_DATA_RETENTION_DAYS", 90)
    org = _org()
    old, new = _call(org, 120), _call(org, 10)
    _run()
    assert _state(old)[3] is None and _state(old)[0] == SECRET_TEXT and _state(old)[2] is True  # transcript policy is separate
    assert _state(new)[3] == "+15551230000"
    assert _audit(org) == [("caller_data", 1, "ok")]


def test_the_audit_trail_holds_counts_only_never_content_or_numbers():
    org = _org()
    _call(org, 60)
    _run()
    db = SessionLocal()
    try:
        row = db.execute(select(DataRetentionRun).where(DataRetentionRun.organization_id == org)).scalar_one()
        dumped = " ".join(str(getattr(row, c.name)) for c in DataRetentionRun.__table__.columns)
    finally:
        db.close()
    assert "4111" not in dumped and "Jane" not in dumped and "+1555" not in dumped
    assert {c.name for c in DataRetentionRun.__table__.columns} == {"id", "created_at", "updated_at", "organization_id", "kind", "cutoff", "rows_affected", "outcome"}


def test_the_log_on_failure_never_contains_call_content(monkeypatch, caplog):
    org = _org()
    _call(org, 60)
    monkeypatch.setattr(retention, "_prune_org", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with caplog.at_level("INFO"):
        _run()
    assert "4111" not in caplog.text and "Jane" not in caplog.text and str(org) not in caplog.text.replace("Traceback", "")


def test_deleting_an_organization_removes_its_audit_rows():
    org = _org()
    _call(org, 60)
    _run()
    from app.db.models.tenancy import Organization

    db = SessionLocal()
    try:
        db.execute(delete(Organization).where(Organization.id == org))  # DB-level cascade, as the account-deletion path relies on
        db.commit()
        assert db.execute(select(func.count()).select_from(DataRetentionRun).where(DataRetentionRun.organization_id == org)).scalar_one() == 0
    finally:
        db.close()


@pytest.mark.parametrize("field,bad", [("TRANSCRIPT_RETENTION_DAYS", -1), ("TRANSCRIPT_RETENTION_DAYS", 99999), ("CALLER_DATA_RETENTION_DAYS", -5),
                                       ("RETENTION_BATCH_SIZE", 0), ("RETENTION_BATCH_SIZE", 10**6), ("RETELL_DATA_RETENTION_DAYS", 0),
                                       ("RETELL_DATA_RETENTION_DAYS", 731)])
def test_unsafe_retention_settings_are_refused_at_start_up(monkeypatch, field, bad):
    monkeypatch.setenv(field, str(bad))
    with pytest.raises(Exception):
        Settings()


def test_blank_retell_retention_means_unset(monkeypatch):
    monkeypatch.setenv("RETELL_DATA_RETENTION_DAYS", "")
    assert Settings().RETELL_DATA_RETENTION_DAYS is None


def test_retell_agent_payload_carries_the_retention_only_when_configured(monkeypatch):
    from app.providers.agent.agent_provider import AgentConfig
    from app.providers.agent.retell_agent_provider import RetellAgentProvider

    cfg = AgentConfig(name="A", language="en-US", voice_provider_id="v", webhook_url="https://x/y", general_prompt="p", begin_message="hi")
    monkeypatch.setattr(get_settings(), "RETELL_DATA_RETENTION_DAYS", None)
    assert "data_storage_retention_days" not in RetellAgentProvider._agent_payload(cfg, "llm")
    monkeypatch.setattr(get_settings(), "RETELL_DATA_RETENTION_DAYS", 90)
    assert RetellAgentProvider._agent_payload(cfg, "llm")["data_storage_retention_days"] == 90


def test_worker_runs_the_retention_job_isolated_from_the_others():
    import inspect

    from app.workers import run_worker

    src = inspect.getsource(run_worker.run_once)
    assert 'isolated("data_retention"' in src and "retention.prune_pass" in src


def test_caller_data_policy_reaches_old_calls_that_have_no_transcript_or_summary(monkeypatch):
    monkeypatch.setattr(get_settings(), "TRANSCRIPT_RETENTION_DAYS", 0)
    monkeypatch.setattr(get_settings(), "CALLER_DATA_RETENTION_DAYS", 90)
    org = _org()
    bare = _call(org, 200, transcript=False, summary=False, rec=False)
    _run()
    assert _state(bare)[3] is None
    assert _audit(org) == [("caller_data", 1, "ok")]
