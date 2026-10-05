from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, BeforeValidator

# Database ids are UUIDs; the API exposes them as strings.
StrId = Annotated[str, BeforeValidator(str)]


class CallOut(BaseModel):
    id: StrId
    direction: str
    caller_number: str | None
    started_at: datetime | None
    ended_at: datetime | None
    duration_seconds: int | None
    status: str | None
    disconnect_reason: str | None
    summary: str | None
    sentiment: str | None

    model_config = {"from_attributes": True}


class LeadOut(BaseModel):
    id: StrId
    name: str | None
    phone: str | None
    email: str | None
    reason: str | None
    summary: str | None
    status: str
    source: str
    created_at: datetime

    model_config = {"from_attributes": True}