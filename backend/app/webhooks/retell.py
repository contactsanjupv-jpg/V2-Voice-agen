"""
POST /webhooks/retell — the single ingress point for all Retell call
events (call_started, call_ended, call_analyzed, transcript_updated, ...).

Security properties enforced here, in order:
  1. Raw-body signature verification (app/webhooks/retell_signature.py)
     BEFORE anything else touches the payload.
  2. Idempotency via the webhook_events table's UNIQUE(source, external_event_id)
     — a duplicate delivery is recorded as a no-op, never double-processed.
  3. Fast 2xx ack, heavy lifting deferred to a background worker (Retell
     times out at 10s and retries on failure/timeout).
"""
from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import get_db
from app.db.models.platform import WebhookEvent, WebhookSource
from app.webhooks.retell_signature import InvalidRetellSignature, verify_retell_signature

router = APIRouter()
settings = get_settings()


@router.post("/webhooks/retell", status_code=status.HTTP_204_NO_CONTENT)
async def handle_retell_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_retell_signature: str | None = Header(default=None),
):
    raw_body = await request.body()

    try:
        verify_retell_signature(
            raw_body=raw_body,
            signature_header=x_retell_signature,
            api_key=settings.RETELL_API_KEY,
            max_skew_seconds=settings.RETELL_WEBHOOK_MAX_SKEW_SECONDS,
        )
    except InvalidRetellSignature:
        # Deliberately generic response — don't tell an attacker *why*
        # verification failed.
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    payload = await request.json()
    event_type = payload.get("event")
    call = payload.get("call", {})
    # Retell events don't carry a single universal "event id" field in
    # every version of their payload; call_id + event name is a stable,
    # sufficiently-unique idempotency key for our purposes. If Retell adds
    # a dedicated event id, prefer that instead.
    external_event_id = f"{call.get('call_id', 'unknown')}:{event_type}"

    event = WebhookEvent(
        source=WebhookSource.retell,
        external_event_id=external_event_id,
        event_type=event_type or "unknown",
        payload=payload,
    )
    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        # UNIQUE constraint hit == we've already seen this exact event.
        # That's success, not an error — ack and stop, do not reprocess.
        db.rollback()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # Enqueue for the background worker (app/workers/retell_events.py) —
    # actual lead/appointment/call-record creation happens there, off the
    # request path, well inside Retell's 10s ack window.
    from app.workers.retell_events import enqueue_event_processing

    enqueue_event_processing(str(event.id))

    return Response(status_code=status.HTTP_204_NO_CONTENT)
