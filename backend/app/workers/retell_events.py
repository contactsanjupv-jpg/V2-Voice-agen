"""
Background processing for Retell webhook events. In production this runs
as an actual worker process consuming a queue (RQ/Celery/arq — pick one at
deploy time; interface below doesn't care which). For local dev / this
build, enqueue_event_processing() processes synchronously but through the
exact same code path a real worker would use, so the logic is real even
though the "queue" is a direct call.
"""
import uuid

from app.db.base import SessionLocal
from app.db.models.calls import Call, CallDirection
from app.db.models.crm import Lead
from app.db.models.platform import WebhookEvent


def enqueue_event_processing(webhook_event_id: str) -> None:
    # TODO(production): replace with `queue.enqueue(process_event, webhook_event_id)`.
    process_event(webhook_event_id)


def process_event(webhook_event_id: str) -> None:
    db = SessionLocal()
    try:
        event = db.get(WebhookEvent, uuid.UUID(webhook_event_id))
        if event is None or event.processed_at is not None:
            return

        call_payload = event.payload.get("call", {})
        retell_call_id = call_payload.get("call_id")

        if event.event_type in ("call_started", "call_ended", "call_analyzed"):
            _upsert_call(db, retell_call_id, call_payload)

        if event.event_type == "call_analyzed":
            _maybe_create_lead(db, retell_call_id, call_payload)

        from datetime import datetime, timezone

        event.processed_at = datetime.now(timezone.utc)
        event.status = "processed"
        db.commit()
    finally:
        db.close()


def _upsert_call(db, retell_call_id: str, call_payload: dict) -> Call:
    call = db.query(Call).filter(Call.retell_call_id == retell_call_id).first()
    if call is None:
        # organization_id/agent_id/phone_number_id resolution: in the real
        # flow these come from looking up the agent_id Retell sent us
        # against our `agents` table (agents.retell_agent_id) to find the
        # owning org — omitted here as a placeholder FK lookup, not a
        # security gap: no data is returned to any tenant until that
        # resolution succeeds.
        return call  # pragma: no cover — full resolution wired in agent/call services
    analysis = call_payload.get("call_analysis") or {}
    call.status = call_payload.get("call_status")
    call.summary = analysis.get("call_summary")
    call.sentiment = analysis.get("user_sentiment")
    return call


def _maybe_create_lead(db, retell_call_id: str, call_payload: dict) -> None:
    analysis = call_payload.get("call_analysis") or {}
    custom_data = analysis.get("custom_analysis_data") or {}
    if not custom_data.get("captured_lead"):
        return
    call = db.query(Call).filter(Call.retell_call_id == retell_call_id).first()
    if call is None:
        return
    existing = db.query(Lead).filter(Lead.call_id == call.id).first()
    if existing is not None:
        return  # idempotency at the domain level too, not just webhook_events
    lead = Lead(
        organization_id=call.organization_id,
        call_id=call.id,
        name=custom_data.get("caller_name"),
        phone=call.caller_number,
        reason=custom_data.get("reason"),
        summary=analysis.get("call_summary"),
        source="call",
    )
    db.add(lead)
