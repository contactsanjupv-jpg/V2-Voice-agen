"""
Paddle Billing implementation. Creates a transaction server-side and
returns Paddle's checkout.url (your default payment link + ?_ptxn=txn_...).
That page must include Paddle.js, which opens the checkout automatically.

organization_id + plan_id travel as custom_data on the transaction; Paddle
copies it onto the resulting subscription, which is how the webhook maps
an event back to a tenant. The client never supplies either value.
"""
import logging

import httpx

from app.config import get_settings
from app.providers.billing.billing_provider import BillingProvider, CheckoutSession

logger = logging.getLogger("atla.billing.paddle")

_BASE_URLS = {"sandbox": "https://sandbox-api.paddle.com", "live": "https://api.paddle.com"}


class PaddleAPIError(RuntimeError):
    """Subclasses RuntimeError so existing `except RuntimeError` blocks map it to a clean 502."""


class PaddleBillingProvider(BillingProvider):
    def __init__(self):
        settings = get_settings()
        if not settings.PADDLE_API_KEY:
            raise RuntimeError("PADDLE_API_KEY is not configured — cannot create checkout sessions.")
        if settings.PADDLE_ENV not in _BASE_URLS:
            raise RuntimeError("PADDLE_ENV must be 'sandbox' or 'live'.")
        self._api_key = settings.PADDLE_API_KEY
        self._base_url = _BASE_URLS[settings.PADDLE_ENV]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    def create_checkout_session(
        self,
        *,
        organization_id: str,
        plan_id: str,
        plan_price_id: str,
        customer_email: str,  # unused: Paddle collects the email in its own checkout
        success_url: str,  # unused here: passed to Paddle.js on the frontend checkout page
        cancel_url: str,
    ) -> CheckoutSession:
        body = {
            "items": [{"price_id": plan_price_id, "quantity": 1}],
            "collection_mode": "automatic",
            "custom_data": {"organization_id": organization_id, "plan_id": plan_id},
        }
        try:
            resp = httpx.post(f"{self._base_url}/transactions", headers=self._headers(), json=body, timeout=15.0)
        except httpx.HTTPError as e:
            raise PaddleAPIError("Could not reach Paddle") from e
        if resp.status_code >= 400:
            logger.error("Paddle create-transaction failed: %s", resp.status_code)
            raise PaddleAPIError(f"Paddle returned {resp.status_code}")
        data = resp.json().get("data") or {}
        url = (data.get("checkout") or {}).get("url")
        if not url or not data.get("id"):
            raise PaddleAPIError("Paddle response missing checkout URL — is a default payment link set?")
        return CheckoutSession(checkout_url=url, provider_session_id=data["id"])

    def cancel_subscription(self, external_subscription_id: str) -> None:
        try:
            resp = httpx.post(
                f"{self._base_url}/subscriptions/{external_subscription_id}/cancel",
                headers=self._headers(),
                json={"effective_from": "immediately"},
                timeout=15.0,
            )
        except httpx.HTTPError as e:
            raise PaddleAPIError("Could not reach Paddle") from e
        if resp.status_code >= 400:
            raise PaddleAPIError(f"Paddle returned {resp.status_code}")