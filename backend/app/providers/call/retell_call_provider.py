"""
Real implementation against Retell's `POST /create-web-call` and
`GET /get-call/{id}` endpoints.
"""
from app.providers.call.call_provider import CallDetail, CallProvider, WebCallSession
from app.providers.retell_client import RetellClient


class RetellCallProvider(CallProvider):
    def __init__(self, client: RetellClient | None = None):
        self.client = client or RetellClient()

    def create_test_call(self, provider_agent_id: str) -> WebCallSession:
        raw = self.client.request("POST", "/create-web-call", json={"agent_id": provider_agent_id})
        return WebCallSession(provider_call_id=raw["call_id"], access_token=raw["access_token"])

    def get_call(self, provider_call_id: str) -> CallDetail:
        raw = self.client.request("GET", f"/get-call/{provider_call_id}")
        return CallDetail(
            provider_call_id=raw.get("call_id", provider_call_id),
            status=raw.get("call_status"),
            started_at=raw.get("start_timestamp"),
            ended_at=raw.get("end_timestamp"),
            duration_seconds=raw.get("duration_ms", 0) // 1000 if raw.get("duration_ms") else None,
            disconnect_reason=raw.get("disconnection_reason"),
            summary=(raw.get("call_analysis") or {}).get("call_summary"),
            sentiment=(raw.get("call_analysis") or {}).get("user_sentiment"),
            recording_url=raw.get("recording_url"),
            transcript=raw.get("transcript_object", []),
            cost_cents=(raw.get("call_cost") or {}).get("combined_cost"),
        )
