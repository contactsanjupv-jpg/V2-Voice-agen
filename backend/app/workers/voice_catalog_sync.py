"""
Mirrors Retell's voice catalog into our `voices` table (shared, not tenant-scoped).

It runs from every path that matters, so a fresh deployment never needs a
manual fix:
  * the worker process (app/workers/run_worker.py) at start-up and every few hours;
  * lazily from GET /voices when the catalog is empty (ensure_voice_catalog).
"""
import logging

from sqlalchemy.orm import Session

from app.core.locks import LockNotAcquired, redis_lock
from app.db.base import SessionLocal
from app.db.models.voice_agent import Voice
from app.providers.voice.retell_voice_provider import RetellVoiceProvider

logger = logging.getLogger("atla.voices")


class VoicesUnavailable(Exception):
    pass


def sync_voice_catalog() -> int:
    provider = RetellVoiceProvider()
    records = [r for r in provider.list_voices() if r.provider_voice_id]
    db = SessionLocal()
    upserted = 0
    try:
        for r in records:
            existing = db.query(Voice).filter(Voice.retell_voice_id == r.provider_voice_id).first()
            if existing is None:
                existing = Voice(retell_voice_id=r.provider_voice_id)
                db.add(existing)
            existing.name = r.name
            existing.provider = r.provider
            existing.gender = r.gender
            existing.accent = r.accent
            existing.age_style = r.age_style
            existing.preview_url = r.preview_url
            existing.voice_metadata = r.metadata
            upserted += 1
        db.commit()
    finally:
        db.close()
    return upserted


def ensure_voice_catalog(db: Session) -> None:
    """If the catalog is empty (fresh database), populate it now. Concurrent
    callers share one sync; failure raises VoicesUnavailable (customer-safe)."""
    if db.query(Voice.id).first() is not None:
        return
    try:
        with redis_lock("voice-sync", ttl_seconds=60):
            db.rollback()  # see rows another process may have just committed
            if db.query(Voice.id).first() is None:
                sync_voice_catalog()
    except LockNotAcquired:
        pass  # another request is syncing; the caller re-reads below
    except Exception as e:  # noqa: BLE001 — provider/network/config errors stay internal
        logger.error("Voice catalog bootstrap failed: %s", e)
        raise VoicesUnavailable() from e
    db.rollback()
    if db.query(Voice.id).first() is None:
        raise VoicesUnavailable()
