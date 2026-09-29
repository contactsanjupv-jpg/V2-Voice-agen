from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class VoiceFilters:
    gender: str | None = None
    accent: str | None = None
    language: str | None = None
    search: str | None = None


@dataclass
class VoiceRecord:
    provider_voice_id: str
    name: str
    provider: str
    gender: str | None
    accent: str | None
    age_style: str | None
    preview_url: str | None
    metadata: dict


class VoiceProvider(ABC):
    @abstractmethod
    def list_voices(self) -> list[VoiceRecord]:
        """Returns the FULL catalog; filtering happens in our service layer
        so we can cache the raw catalog and filter cheaply per-request."""
        ...
