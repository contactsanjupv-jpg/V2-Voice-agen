"""
Scheduled job (run hourly/daily — voices change rarely): pulls the live
Retell voice catalog and upserts it into our `voices` table. Not
tenant-scoped — one shared sync for the whole platform.
"""
from app.db.base import SessionLocal
from app.db.models.voice_agent import Voice
from app.providers.voice.retell_voice_provider import RetellVoiceProvider


def sync_voice_catalog() -> int:
    provider = RetellVoiceProvider()
    records = provider.list_voices()
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
