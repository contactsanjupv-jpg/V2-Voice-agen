from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class WebCallSession:
    """Everything the browser needs to join one web call (Retell v3 create-web-call response)."""

    provider_call_id: str
    access_token: str  # authorizes the browser to join; short-lived, never logged or stored
    transport: str = "gateway"  # which client transport to use ("gateway" in v3; the SDK defaults to livekit if omitted!)
    ice_servers: list[dict] = field(default_factory=list)  # may carry TURN credentials: never logged or stored
    expires_at: int | None = None  # epoch milliseconds when access_token stops working
    url: str | None = None  # only for transports that need one (livekit)


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
