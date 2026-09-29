"""
Real implementation against Stripe's Checkout Sessions API. Uses Stripe's
own hosted payment page — we never collect or touch a raw card number
ourselves.

organization_id AND plan_id travel as metadata on both the checkout
session and the resulting subscription object — that's how the webhook
handler maps a Stripe event back to one of our tenants and knows which
plan was purchased, without a second API call.
"""
import stripe

from app.config import get_settings
from app.providers.billing.billing_provider import BillingProvider, CheckoutSession


class StripeBillingProvider(BillingProvider):
    def __init__(self):
        settings = get_settings()
        if not settings.STRIPE_SECRET_KEY:
            raise RuntimeError("STRIPE_SECRET_KEY is not configured — cannot create checkout sessions.")
        stripe.api_key = settings.STRIPE_SECRET_KEY

    def create_checkout_session(
        self,
        *,
        organization_id: str,
        plan_id: str,
        plan_price_id: str,
        customer_email: str,
        success_url: str,
        cancel_url: str,
    ) -> CheckoutSession:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": plan_price_id, "quantity": 1}],
            customer_email=customer_email,
            success_url=success_url,
            cancel_url=cancel_url,
            client_reference_id=organization_id,
            metadata={"organization_id": organization_id, "plan_id": plan_id},
            subscription_data={"metadata": {"organization_id": organization_id, "plan_id": plan_id}},
        )
        return CheckoutSession(checkout_url=session.url, provider_session_id=session.id)

    def cancel_subscription(self, external_subscription_id: str) -> None:
        stripe.Subscription.delete(external_subscription_id)