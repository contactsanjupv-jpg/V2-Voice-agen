"""
Voices are a shared catalog mirrored from Retell. This endpoint reads OUR cached
copy; the worker refreshes it, and an empty catalog is bootstrapped on first use.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.db.base import get_db
from app.db.models.voice_agent import Voice
from app.schemas.catalog import VoiceOut
from app.workers.voice_catalog_sync import VoicesUnavailable, ensure_voice_catalog

router = APIRouter(prefix="/api/v1/voices", tags=["voices"])


@router.get("", response_model=list[VoiceOut])
def list_voices(
    gender: str | None = Query(default=None),
    accent: str | None = Query(default=None),
    search: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    try:
        ensure_voice_catalog(db)  # fresh deployment: populate from the provider instead of failing
    except VoicesUnavailable as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Voices are temporarily unavailable. Please try again in a moment.") from e
    query = db.query(Voice)
    if gender:
        query = query.filter(Voice.gender == gender)
    if accent:
        query = query.filter(Voice.accent == accent)
    if search:
        query = query.filter(Voice.name.ilike(f"%{search}%"))
    voices = query.order_by(Voice.name).all()
    return voices
