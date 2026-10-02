import re

from pydantic import BaseModel, Field, field_validator, model_validator

_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
# The only behaviours the product genuinely supports. Anything else the
# client sends (e.g. the old "book_appointments") is dropped, not stored.
SUPPORTED_TASKS = ("answer_questions", "capture_leads", "take_messages", "transfer_calls")
PERSONALITIES = ("friendly", "professional", "concise")


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
    name: str = Field(min_length=1, max_length=255)
    greeting: str | None = Field(default=None, max_length=500)
    personality: str = "professional"
    language: str = "en-US"
    voice_id: str  # our internal Voice.id (uuid)
    tasks: dict
    transfer_number: str | None = None
    business_hours: dict | None = None  # reserved; not customer-configurable yet
    after_hours_behavior: str | None = None  # reserved; not customer-configurable yet

    @field_validator("personality")
    @classmethod
    def _personality_known(cls, v: str) -> str:
        v = v.lower()
        if v not in PERSONALITIES:
            raise ValueError(f"personality must be one of {', '.join(PERSONALITIES)}")
        return v

    @field_validator("tasks")
    @classmethod
    def _tasks_supported_only(cls, v: dict) -> dict:
        return {key: bool(v.get(key, False)) for key in SUPPORTED_TASKS}

    @field_validator("transfer_number")
    @classmethod
    def _transfer_e164(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = re.sub(r"[\s().-]", "", v.strip())
        if not _E164.match(v):
            raise ValueError("Transfer number must be a full phone number like +14155551234")
        return v

    @model_validator(mode="after")
    def _transfer_needs_number(self):
        if self.tasks.get("transfer_calls") and not self.transfer_number:
            raise ValueError("Enter a transfer number, or turn off call transfer")
        if not self.tasks.get("transfer_calls"):
            self.transfer_number = None
        return self


class AgentOut(BaseModel):
    id: str
    name: str
    status: str
    retell_agent_id: str | None
    # True only when the voice provider has received the latest saved config.
    synced: bool = False

    model_config = {"from_attributes": True}
