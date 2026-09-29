from pydantic import BaseModel


class VoiceOut(BaseModel):
    id: str
    retell_voice_id: str
    name: str
    provider: str
    gender: str | None
    accent: str | None
    age_style: str | None
    preview_url: str | None

    model_config = {"from_attributes": True}


class PhoneNumberOut(BaseModel):
    id: str
    number: str
    area_code: str | None
    country: str
    monthly_cost_cents: int | None
    status: str

    model_config = {"from_attributes": True}


class PurchaseNumberRequest(BaseModel):
    country: str = "US"
    area_code: str | None = None


class AgentConfigRequest(BaseModel):
    name: str
    greeting: str | None = None
    personality: str = "professional"
    language: str = "en-US"
    voice_id: str  # our internal Voice.id (uuid)
    tasks: dict
    transfer_number: str | None = None
    business_hours: dict | None = None
    after_hours_behavior: str | None = None


class AgentOut(BaseModel):
    id: str
    name: str
    status: str
    retell_agent_id: str | None

    model_config = {"from_attributes": True}
