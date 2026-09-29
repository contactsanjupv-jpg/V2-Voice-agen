"""
Real implementation against Retell's documented `GET /list-voices` endpoint
(docs.retellai.com/api-references/list-voices). Response shape below is
Retell's documented voice object; if their schema adds fields we don't
recognize, we pass them through into `metadata` rather than dropping them.
"""
from app.providers.retell_client import RetellClient
from app.providers.voice.voice_provider import VoiceProvider, VoiceRecord


class RetellVoiceProvider(VoiceProvider):
    def __init__(self, client: RetellClient | None = None):
        self.client = client or RetellClient()

    def list_voices(self) -> list[VoiceRecord]:
        raw = self.client.request("GET", "/list-voices")
        voices = raw if isinstance(raw, list) else raw.get("voices", [])
        return [self._to_record(v) for v in voices]

    @staticmethod
    def _to_record(v: dict) -> VoiceRecord:
        return VoiceRecord(
            provider_voice_id=v.get("voice_id"),
            name=v.get("voice_name", "Unnamed voice"),
            provider=v.get("provider", "unknown"),
            gender=v.get("gender"),
            accent=v.get("accent"),
            age_style=v.get("age"),
            preview_url=v.get("preview_audio_url"),
            metadata=v,
        )
