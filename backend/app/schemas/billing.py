from pydantic import BaseModel


class CreateCheckoutSessionRequest(BaseModel):
    plan: str  # "starter" | "growth"


class CreateCheckoutSessionResponse(BaseModel):
    checkout_url: str


class SubscriptionOut(BaseModel):
    plan_id: str
    status: str

    model_config = {"from_attributes": True}