"""
Real implementation against Retell's v3 create-web-call and get-call endpoints.

Retell has retired /v2/create-web-call (HTTP 400, "deprecated API"), so web calls are created with
POST /v3/create-web-call ONLY. There is no fallback to another version and no retry on a different path: a refusal
from Retell is a real answer (nothing was created) and is surfaced as such.

v3 response (V3CreateWebCallResponse, all required): call_id, access_token, transport ("gateway"), ice_servers
(RTCIceServer-shaped: urls, username?, credential?) and expires_at (epoch ms). The browser SDK needs the call_id,
transport and ice_servers as well as the token, so all of them are returned to the frontend. get-call is not used by
any flow yet.
"""
import logging

from app.providers.call.call_provider import CallDetail, CallProvider, WebCallSession
from app.providers.retell_client import RetellAPIError, RetellClient

logger = logging.getLogger("atla.retell.calls")

CREATE_WEB_CALL_PATH = "/v3/create-web-call"
_TRANSPORTS = ("gateway", "livekit")  # what the installed retell-client-js-sdk can speak


def _malformed(reason: str) -> RetellAPIError:
    # Retell answered 2xx, so a call MAY now exist even though we can't hand it to the browser: ambiguous, so the
    # caller keeps a free test spent rather than risk paying twice. The body is deliberately NOT attached (it holds
    # the access token and TURN credentials).
    return RetellAPIError(502, f"Unusable create-web-call response: {reason}", None, ambiguous=True)


def _ice_server(entry: object) -> dict:
    if not isinstance(entry, dict):
        raise _malformed("ice_servers entry is not an object")
    urls = entry.get("urls")
    if isinstance(urls, str):
        ok = bool(urls)
    else:
        ok = isinstance(urls, list) and bool(urls) and all(isinstance(u, str) and u for u in urls)
    if not ok:
        raise _malformed("ice_servers entry has no urls")
    out: dict = {"urls": urls}
    for key in ("username", "credential"):
        if entry.get(key) is not None:
            if not isinstance(entry[key], str):
                raise _malformed(f"ice_servers {key} is not a string")
            out[key] = entry[key]
    return out


def parse_web_call(raw: object) -> WebCallSession:
    """Validate Retell's create-web-call answer and keep every connection field the browser needs."""
    if not isinstance(raw, dict):
        raise _malformed("not a JSON object")
    call_id, token, transport = raw.get("call_id"), raw.get("access_token"), raw.get("transport")
    if not isinstance(call_id, str) or not call_id:
        raise _malformed("missing call_id")
    if not isinstance(token, str) or not token:
        raise _malformed("missing access_token")
    if transport not in _TRANSPORTS:
        raise _malformed("unsupported or missing transport")
    url = raw.get("url")
    if transport == "livekit" and not (isinstance(url, str) and url):
        raise _malformed("livekit transport without a url")
    servers = raw.get("ice_servers")
    if servers is None:
        logger.warning("Retell create-web-call returned no ice_servers; the browser will use its defaults")
        servers = []
    if not isinstance(servers, list):
        raise _malformed("ice_servers is not a list")
    expires_at = raw.get("expires_at")
    if expires_at is not None and (isinstance(expires_at, bool) or not isinstance(expires_at, int)):
        raise _malformed("expires_at is not an integer")
    return WebCallSession(
        provider_call_id=call_id,
        access_token=token,
        transport=transport,
        ice_servers=[_ice_server(e) for e in servers],
        expires_at=expires_at,
        url=url if isinstance(url, str) and url else None,
    )


class RetellCallProvider(CallProvider):
    def __init__(self, client: RetellClient | None = None):
        self.client = client or RetellClient()

    def create_test_call(self, provider_agent_id: str, metadata: dict | None = None) -> WebCallSession:
        body: dict = {"agent_id": provider_agent_id}
        if metadata:
            body["metadata"] = metadata  # stored by Retell with the call; correlation uses call_id, not this
        raw = self.client.request("POST", CREATE_WEB_CALL_PATH, json=body)
        logger.info("Retell create-web-call succeeded via %s", CREATE_WEB_CALL_PATH)
        return parse_web_call(raw)

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
