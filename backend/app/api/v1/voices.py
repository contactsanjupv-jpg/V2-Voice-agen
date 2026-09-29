"""
Voices are a shared, non-tenant-scoped catalog mirrored from Retell (see
app/db/models/voice_agent.py). This endpoint reads OUR cached copy — a
background job (app/workers/voice_catalog_sync.py, refresh on a schedule)
is what actually calls RetellVoiceProvider.list_voices() and upserts it,
so a customer browsing voices never waits on a live Retell round trip and
we're not making N Retell calls for N concurrent customers browsing voices.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.db.base import get_db
from app.db.models.voice_agent import Voice
from app.schemas.catalog import VoiceOut

router = APIRouter(prefix="/api/v1/voices", tags=["voices"])


@router.get("", response_model=list[VoiceOut])
def list_voices(
    gender: str | None = Query(default=None),
    accent: str | None = Query(default=None),
    search: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    query = db.query(Voice)
    if gender:
        query = query.filter(Voice.gender == gender)
    if accent:
        query = query.filter(Voice.accent == accent)
    if search:
        query = query.filter(Voice.name.ilike(f"%{search}%"))
    voices = query.order_by(Voice.name).all()
    return [VoiceOut(**{**v.__dict__, "id": str(v.id)}) for v in voices]
