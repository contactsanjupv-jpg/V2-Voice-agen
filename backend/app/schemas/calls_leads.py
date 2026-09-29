from datetime import datetime

from pydantic import BaseModel


class CallOut(BaseModel):
    id: str
    direction: str
    caller_number: str | None
    started_at: datetime | None
    ended_at: datetime | None
    duration_seconds: int | None
    status: str | None
    disconnect_reason: str | None
    summary: str | None
    sentiment: str | None
    cost_cents: int | None

    model_config = {"from_attributes": True}


class LeadOut(BaseModel):
    id: str
    name: str | None
    phone: str | None
    email: str | None
    reason: str | None
    summary: str | None
    status: str
    source: str
    created_at: datetime

    model_config = {"from_attributes": True}