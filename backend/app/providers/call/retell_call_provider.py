"""
Real implementation against Retell's create-web-call and get-call endpoints.

Retell versions its call endpoints; the unversioned `/create-web-call` returns
404 ("Cannot POST /create-web-call"). Retell's official SDK config uses
/v2/create-web-call while the current docs page example shows /v3. A 404 means
the route does not exist (nothing was created), so trying the next version is
safe. UNVERIFIED which one is canonical today: the first successful path is
logged once so it can be pinned. get-call is not used by any flow yet.
"""
import logging

from app.providers.call.call_provider import CallDetail, CallProvider, WebCallSession
from app.providers.retell_client import RetellAPIError, RetellClient

logger = logging.getLogger("atla.retell.calls")

CREATE_WEB_CALL_PATHS = ("/v2/create-web-call", "/v3/create-web-call")


class RetellCallProvider(CallProvider):
    def __init__(self, client: RetellClient | None = None):
        self.client = client or RetellClient()

    def _create_web_call(self, body: dict) -> dict:
        last_404: RetellAPIError | None = None
        for path in CREATE_WEB_CALL_PATHS:
            try:
                raw = self.client.request("POST", path, json=body)
            except RetellAPIError as e:
                if e.status_code != 404:
                    raise  # a real answer from Retell (auth, validation, ...) — never retried on another path
                logger.warning("Retell route not found: POST %s (trying the next version)", path)
                last_404 = e
                continue
            logger.info("Retell create-web-call succeeded via %s", path)
            return raw
        raise last_404  # every known version returned 404
    
    def create_test_call(self, provider_agent_id: str, metadata: dict | None = None) -> WebCallSession:
        body: dict = {"agent_id": provider_agent_id}
        if metadata:
            body["metadata"] = metadata  # echoed back in Retell's webhook payloads
        raw = self._create_web_call(body)
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
