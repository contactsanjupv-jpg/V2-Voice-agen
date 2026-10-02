from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class WebCallSession:
    provider_call_id: str
    access_token: str  # handed to the frontend so it can join the WebRTC call


@dataclass
class CallDetail:
    provider_call_id: str
    status: str | None
    started_at: str | None
    ended_at: str | None
    duration_seconds: int | None
    disconnect_reason: str | None
    summary: str | None
    sentiment: str | None
    recording_url: str | None
    transcript: list[dict]
    cost_cents: int | None


class CallProvider(ABC):
    @abstractmethod
    def create_test_call(self, provider_agent_id: str, metadata: dict | None = None) -> WebCallSession: ...        
    """Browser-based WebRTC test call — no telephony hop, no real phone
    number consumed. This is what backs the "Test AI" button."""
    ...

    @abstractmethod
    def get_call(self, provider_call_id: str) -> CallDetail: ...
