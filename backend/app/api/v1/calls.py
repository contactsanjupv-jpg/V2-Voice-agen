"""
Read-only calls endpoints. Every query below filters by
membership.organization_id — the same tenant-isolation pattern as every
other route in this app (see app/auth/deps.py:current_membership). A call
belonging to another org returns 404, never 403, so existence is never
confirmed to a non-member either.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.auth.deps import current_membership
from app.db.base import get_db
from app.db.models.calls import Call, CallTranscript
from app.db.models.tenancy import OrganizationMember
from app.schemas.calls_leads import CallOut

router = APIRouter(prefix="/api/v1/orgs/{organization_id}/calls", tags=["calls"])

MAX_PAGE_SIZE = 100


@router.get("", response_model=list[CallOut])
def list_calls(
    limit: int = Query(default=25, le=MAX_PAGE_SIZE, gt=0),
    offset: int = Query(default=0, ge=0),
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    calls = (
        db.query(Call)
        .filter(Call.organization_id == membership.organization_id)
        .order_by(Call.created_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )
    return [CallOut.model_validate(c) for c in calls]


@router.get("/{call_id}", response_model=CallOut)
def get_call(
    call_id: uuid.UUID,
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    call = db.query(Call).filter(Call.id == call_id, Call.organization_id == membership.organization_id).first()
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Call not found")
    return CallOut.model_validate(call)


@router.get("/{call_id}/transcript")
def get_call_transcript(
    call_id: uuid.UUID,
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    call = db.query(Call).filter(Call.id == call_id, Call.organization_id == membership.organization_id).first()
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Call not found")

    transcript = db.query(CallTranscript).filter(CallTranscript.call_id == call.id).first()
    if transcript is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No transcript available for this call")
    return {"call_id": str(call.id), "transcript": transcript.transcript}


# Recording playback intentionally not exposed yet: spec requires private
# storage + short-lived signed URLs (never a raw storage key or public
# link), and no storage/signing provider is wired up yet. Add a
# GET /{call_id}/recording-url endpoint that generates a signed URL
# on demand once that provider exists — do not add a field that returns
# `recording_storage_key` directly to the frontend in the meantime.