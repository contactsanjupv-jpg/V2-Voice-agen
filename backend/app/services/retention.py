"""
Automatic, repeatable pruning of call content and caller data (docs/DATA_RETENTION.md).

Two independent policies, both OFF until the owner sets them (0 = keep):
  TRANSCRIPT_RETENTION_DAYS   deletes the stored transcript and clears the summary and recording storage key.
  CALLER_DATA_RETENTION_DAYS  clears the caller's phone number on the call record.
The call row itself (times, duration, status, cost) is kept: usage, credits and billing reconcile against it.
Age is measured from when the call started (or was created), so a call is never pruned early.

Properties: every statement is scoped by organization_id; each organization is processed in its own transaction, so one failure
rolls back only that organization, is recorded in the audit table, and is retried on the next pass; passes are idempotent
(a pruned call no longer matches); work proceeds in bounded batches. The audit table stores counts only, never content.
This prunes Atla's own database. It cannot reach what Retell stores; that is governed by RETELL_DATA_RETENTION_DAYS.
"""
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models.calls import Call, CallTranscript
from app.db.models.retention import DataRetentionRun

logger = logging.getLogger("atla.retention")

ORG_PAGE = 200
_last_pass: float | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age():
    return func.coalesce(Call.started_at, Call.created_at)


def _has_content():
    return or_(Call.summary.is_not(None), Call.recording_storage_key.is_not(None), exists().where(CallTranscript.call_id == Call.id))


def _eligible(kind: str, cutoff: datetime):
    cond = _has_content() if kind == "transcripts" else Call.caller_number.is_not(None)
    return and_(_age() < cutoff, cond)


def _record(db: Session, org: uuid.UUID, kind: str, cutoff: datetime, rows: int, outcome: str) -> None:
    db.add(DataRetentionRun(organization_id=org, kind=kind, cutoff=cutoff, rows_affected=rows, outcome=outcome))
    db.commit()


def _prune_org(db: Session, org: uuid.UUID, kind: str, cutoff: datetime, batch: int) -> int:
    total = 0
    previous: list | None = None
    while True:
        ids = db.execute(select(Call.id).where(Call.organization_id == org, _eligible(kind, cutoff)).order_by(Call.id).limit(batch)).scalars().all()
        if not ids or ids == previous:  # an identical batch twice means the update did not take: stop rather than loop forever
            return total
        previous = ids
        if kind == "transcripts":
            db.execute(delete(CallTranscript).where(CallTranscript.call_id.in_(select(Call.id).where(Call.id.in_(ids), Call.organization_id == org))))
            db.execute(update(Call).where(Call.id.in_(ids), Call.organization_id == org).values(summary=None, recording_storage_key=None))
        else:
            db.execute(update(Call).where(Call.id.in_(ids), Call.organization_id == org).values(caller_number=None))
        db.commit()
        total += len(ids)


def prune(db: Session, now: datetime | None = None) -> dict:
    """One pass over every organization with something past its retention window. Never raises."""
    cfg = get_settings()
    now = now or _now()
    policies = [(k, d) for k, d in (("transcripts", cfg.TRANSCRIPT_RETENTION_DAYS), ("caller_data", cfg.CALLER_DATA_RETENTION_DAYS)) if d]
    summary: dict = {"transcripts": 0, "caller_data": 0, "failed_orgs": 0}
    for kind, days in policies:
        cutoff = now - timedelta(days=days)
        last = None
        while True:
            q = select(Call.organization_id).where(_eligible(kind, cutoff)).group_by(Call.organization_id).order_by(Call.organization_id).limit(ORG_PAGE)
            if last is not None:
                q = q.where(Call.organization_id > last)
            orgs = db.execute(q).scalars().all()
            if not orgs:
                break
            for org in orgs:
                try:
                    n = _prune_org(db, org, kind, cutoff, cfg.RETENTION_BATCH_SIZE)
                    if n:
                        _record(db, org, kind, cutoff, n, "ok")
                        summary[kind] += n
                except Exception:  # noqa: BLE001 - one organization must never stop the others; retried next pass
                    db.rollback()
                    summary["failed_orgs"] += 1
                    logger.exception("Retention pass failed for one organization (kind=%s); it will be retried", kind)  # ids stay out of the log
                    try:
                        _record(db, org, kind, cutoff, 0, "failed")
                    except Exception:  # noqa: BLE001
                        db.rollback()
            last = orgs[-1]
    return summary


def prune_pass(db: Session) -> dict | None:
    """Hourly gate for the worker loop. Returns None when nothing was due or nothing changed."""
    global _last_pass
    cfg = get_settings()
    if not (cfg.TRANSCRIPT_RETENTION_DAYS or cfg.CALLER_DATA_RETENTION_DAYS):
        return None
    if _last_pass is not None and time.monotonic() - _last_pass < 3600:
        return None
    _last_pass = time.monotonic()
    out = prune(db)
    return out if (out["transcripts"] or out["caller_data"] or out["failed_orgs"]) else None
