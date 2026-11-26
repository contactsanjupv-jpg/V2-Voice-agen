"""Daily analytics report, IN-APP ONLY (there is no analytics email, and none may be added).

Once a day, after the start of a business's local morning, each organization that had real activity on its previous local day gets
ONE `daily_report` notification (calls, call minutes, new leads, and the day before for comparison). The figures use exactly the
dashboard's rules: customer calls only (test calls excluded), calls dated by when they started, leads by when they were created, a
zero is a true zero, nothing is estimated. A day with no calls and no leads produces no report (no noise).

Idempotent and safe to run any number of times or from several workers: the notification key is the local date, so the database
admits one report per recipient per day. Days missed while the worker was down are not back-filled (a stale report is not news).
The day's end is the organization's local midnight (its business timezone; UTC when none is set).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.business import Business
from app.db.models.calls import Call, CallDirection
from app.db.models.crm import Lead
from app.db.models.notifications import Notification
from app.db.models.tenancy import Organization
from app.services import notifications
from app.services.credits import resolve_timezone

logger = logging.getLogger("atla.daily_analytics")

REPORT_HOUR_LOCAL = 8  # the report for yesterday appears from 08:00 local time
LOOKBACK = timedelta(hours=72)  # only organizations with activity in this window can have a report due
BATCH_SIZE = 500  # organizations per keyset page (a page size, not a cap: the pass continues until every eligible organization is visited)


def _call_time():
    return func.coalesce(Call.started_at, Call.created_at)


def day_totals(db: Session, org_id: uuid.UUID, start_utc: datetime, end_utc: datetime) -> tuple[int, int, int]:
    """(calls, call_seconds, leads) in [start, end): the dashboard's own definition."""
    calls, seconds = (
        db.query(func.count(Call.id), func.coalesce(func.sum(Call.duration_seconds), 0))
        .filter(Call.organization_id == org_id, Call.direction != CallDirection.test, _call_time() >= start_utc, _call_time() < end_utc)
        .one()
    )
    leads = db.query(func.count(Lead.id)).filter(Lead.organization_id == org_id, Lead.created_at >= start_utc, Lead.created_at < end_utc).scalar()
    return int(calls), int(seconds), int(leads)


def organization_timezone(db: Session, org_id: uuid.UUID) -> str | None:
    return db.execute(
        select(Business.timezone).where(Business.organization_id == org_id, Business.timezone.is_not(None)).order_by(Business.created_at).limit(1)
    ).scalar_one_or_none()


def report_for(db: Session, org_id: uuid.UUID, now: datetime | None = None, force: bool = False) -> bool:
    """Create yesterday's report for one organization if it is due. True if a notification was created."""
    now = now or datetime.now(timezone.utc)
    zone = resolve_timezone(organization_timezone(db, org_id))
    local_now = now.astimezone(zone)
    if not force and local_now.hour < REPORT_HOUR_LOCAL:
        return False
    day = local_now.date() - timedelta(days=1)
    key = f"daily:{day.isoformat()}"
    already = db.execute(
        select(Notification.id).where(Notification.organization_id == org_id, Notification.kind == "daily_report", Notification.dedupe_key == key).limit(1)
    ).first()
    if already is not None:
        return False

    def midnight(d):
        return datetime.combine(d, time.min, tzinfo=zone).astimezone(timezone.utc)

    start, end = midnight(day), midnight(day + timedelta(days=1))
    calls, seconds, leads = day_totals(db, org_id, start, end)
    if calls == 0 and leads == 0:
        return False
    before_calls, _s, _l = day_totals(db, org_id, midnight(day - timedelta(days=1)), start)
    return notifications.create(
        db, org_id, "daily_report", key,
        {"date": day.isoformat(), "calls": calls, "minutes": (seconds + 30) // 60, "leads": leads, "previous_calls": before_calls},
    )


def _eligible_batch(db: Session, since: datetime, after: uuid.UUID | None) -> list[uuid.UUID]:
    """The next BATCH_SIZE organization ids, in ascending id order, that had customer activity since `since` and sort after `after`.
    Keyset pagination over the organizations primary key: stable while rows come and go, never skips or repeats an organization,
    and each page is an index range scan that stops after BATCH_SIZE rows."""
    recent_call = (
        select(Call.id).where(Call.organization_id == Organization.id, Call.direction != CallDirection.test, _call_time() >= since).exists()
    )
    recent_lead = select(Lead.id).where(Lead.organization_id == Organization.id, Lead.created_at >= since).exists()
    stmt = select(Organization.id).where(recent_call | recent_lead).order_by(Organization.id).limit(BATCH_SIZE)
    if after is not None:
        stmt = stmt.where(Organization.id > after)
    return [r[0] for r in db.execute(stmt).all()]


def report_pass(db: Session, now: datetime | None = None) -> int:
    """The worker's hourly pass over EVERY organization with recent activity, one keyset page at a time. Returns how many reports
    were created. There is no cap: a report that is due stays due until the organization's local day rolls over (many hours), and
    every hourly pass visits every eligible organization from the start, so nobody can be starved by their position.
    Each organization is committed on its own, so a failure (logged, rolled back) never stops the pass or loses anyone else's work,
    and the cursor advances past it either way. Concurrent workers are safe: the notification key admits one report per day."""
    now = now or datetime.now(timezone.utc)
    since = now - LOOKBACK
    created, after = 0, None
    while True:
        batch = _eligible_batch(db, since, after)
        if not batch:
            return created
        for org_id in batch:
            try:
                if report_for(db, org_id, now):
                    created += 1
                db.commit()
            except Exception:  # noqa: BLE001 - isolate: a bad organization must not stop the pass
                db.rollback()
                logger.exception("Daily report failed for org %s", org_id)
        after = batch[-1]
        if len(batch) < BATCH_SIZE:
            return created
