"""
Durable processing of Retell webhook events. The webhook stores the event
first (so nothing is lost), then calls process_event(). The same function is
what the sweeper re-runs, so there is exactly one code path.

State machine on webhook_events.status:
    received -> processed
    received -> failed (attempt count, backoff) -> processed
    failed   -> dead   (after RETELL_EVENT_MAX_ATTEMPTS, or malformed)
An event is marked processed ONLY after ingest_event() returned without error
inside a savepoint, so partial writes from a failed attempt are rolled back.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.platform import WebhookEvent, WebhookSource
from app.services.call_ingestion import InvalidEvent, ingest_event

logger = logging.getLogger("atla.workers.retell")
settings = get_settings()

_BACKOFF_BASE_SECONDS = 30
_BACKOFF_CAP_SECONDS = 3600
_RECEIVED_GRACE_SECONDS = 30  # don't race the inline attempt made by the webhook itself


def _now() -> datetime:
    return datetime.now(timezone.utc)


def process_event(event_id: str) -> str:
    """Process one event. Returns its resulting status. Never raises."""
    db = SessionLocal()
    try:
        event = (
            db.query(WebhookEvent)
            .filter(WebhookEvent.id == uuid.UUID(str(event_id)))
            .with_for_update(skip_locked=True)  # a concurrent worker holding it => skip
            .first()
        )
        if event is None:
            return "skipped"
        if event.status in ("processed", "dead"):
            return event.status

        event.attempts = (event.attempts or 0) + 1
        try:
            with db.begin_nested():
                ingest_event(db, event)
        except InvalidEvent as exc:
            event.status, event.last_error, event.next_attempt_at = "dead", f"InvalidEvent: {exc}"[:500], None
        except Exception as exc:  # noqa: BLE001 — every failure must be recorded, never swallowed
            logger.exception("Retell event %s failed (attempt %s)", event.external_event_id, event.attempts)
            event.last_error = f"{type(exc).__name__}: {exc}"[:500]
            if event.attempts >= settings.RETELL_EVENT_MAX_ATTEMPTS:
                event.status, event.next_attempt_at = "dead", None
            else:
                delay = min(_BACKOFF_BASE_SECONDS * 2 ** (event.attempts - 1), _BACKOFF_CAP_SECONDS)
                event.status, event.next_attempt_at = "failed", _now() + timedelta(seconds=delay)
        else:
            event.status, event.processed_at, event.last_error, event.next_attempt_at = "processed", _now(), None, None
        db.commit()
        return event.status
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("Could not process Retell event %s", event_id)
        return "error"
    finally:
        db.close()


def enqueue_event_processing(event_id: str) -> None:
    """Called by the webhook right after the event is stored. The event is
    already durable, so a failure here is recoverable by the sweeper."""
    process_event(event_id)


def retry_due_events(limit: int = 50) -> int:
    """Sweeper: re-run failed events whose backoff elapsed, and events that were
    stored but never attempted (process died between commit and processing)."""
    db = SessionLocal()
    try:
        now = _now()
        ids = [
            row.id
            for row in db.query(WebhookEvent.id)
            .filter(
                WebhookEvent.source == WebhookSource.retell,
                or_(
                    and_(WebhookEvent.status == "failed", WebhookEvent.next_attempt_at <= now),
                    and_(WebhookEvent.status == "received", WebhookEvent.created_at <= now - timedelta(seconds=_RECEIVED_GRACE_SECONDS)),
                ),
            )
            .order_by(WebhookEvent.created_at)
            .limit(limit)
            .all()
        ]
    finally:
        db.close()
    for event_id in ids:
        process_event(str(event_id))
    return len(ids)


def requeue_dead(event_id: str | None = None) -> int:
    """Operator action: give dead events a fresh set of attempts."""
    db = SessionLocal()
    try:
        q = db.query(WebhookEvent).filter(WebhookEvent.source == WebhookSource.retell, WebhookEvent.status == "dead")
        if event_id:
            q = q.filter(WebhookEvent.id == uuid.UUID(event_id))
        count = 0
        for event in q.all():
            event.status, event.attempts, event.next_attempt_at = "failed", 0, _now()
            count += 1
        db.commit()
        return count
    finally:
        db.close()
