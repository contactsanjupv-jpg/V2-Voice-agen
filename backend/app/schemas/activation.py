from pydantic import BaseModel


class IceServer(BaseModel):
    """One RTCIceServer. Absent optional fields are omitted from the JSON (a null username would break WebRTC)."""

    urls: str | list[str]
    username: str | None = None
    credential: str | None = None


class TestCallResponse(BaseModel):
    call_id: str  # also the SDK's callId: the gateway transport refuses to connect without it
    access_token: str  # handed to the frontend's WebRTC client to join the call
    transport: str  # required: the SDK defaults to livekit when it is omitted, which cannot use a v3 token
    ice_servers: list[IceServer] = []
    expires_at: int | None = None  # epoch ms when access_token expires
    url: str | None = None
    max_seconds: int  # the browser ends the call after this long (server-configured)
    is_free: bool = False  # True when this was the one promised free test call


class ActivateRequest(BaseModel):
    phone_number_id: str
    agent_id: str


class ActivateResponse(BaseModel):
    phone_number_id: str
    agent_id: str
    status: str
