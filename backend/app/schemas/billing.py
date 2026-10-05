from datetime import datetime

from pydantic import BaseModel


class CreateCheckoutSessionRequest(BaseModel):
    plan: str  # "starter" | "growth"


class CreateCheckoutSessionResponse(BaseModel):
    checkout_url: str


class SubscriptionOut(BaseModel):
    plan_id: str
    status: str
    current_period_end: datetime | None = None
    # Set when the customer has cancelled but service continues until this date.
    cancel_effective_at: datetime | None = None

    model_config = {"from_attributes": True}