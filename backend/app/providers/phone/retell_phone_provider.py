"""
Real implementation against Retell's documented `POST /create-phone-number`
endpoint. Confirmed directly against their docs: this single call both
selects AND buys a number — area_code is a request parameter Retell tries
to honor, there is no separate 'search first' step. update-phone-number
handles agent assignment after the fact; delete-phone-number releases it.
"""
from app.providers.phone.phone_provider import PhoneProvider, ProvisionedNumber
from app.providers.retell_client import RetellClient


class RetellPhoneProvider(PhoneProvider):
    def __init__(self, client: RetellClient | None = None):
        self.client = client or RetellClient()

    def purchase_number(self, country: str, area_code: str | None) -> ProvisionedNumber:
        payload: dict = {"country_code": country}
        if area_code:
            payload["area_code"] = int(area_code)

        raw = self.client.request("POST", "/create-phone-number", json=payload)
        return ProvisionedNumber(
            provider_phone_number_id=raw["phone_number"],
            number=raw["phone_number"],
            area_code=area_code,
            country=country,
            monthly_cost_cents=raw.get("monthly_cost_cents"),
        )

    def assign_agent(self, provider_phone_number_id: str, provider_agent_id: str, direction: str = "inbound") -> None:
        field = "inbound_agent_id" if direction == "inbound" else "outbound_agent_id"
        self.client.request(
            "PATCH",
            f"/update-phone-number/{provider_phone_number_id}",
            json={field: provider_agent_id},
        )

    def release_number(self, provider_phone_number_id: str) -> None:
        self.client.request("DELETE", f"/delete-phone-number/{provider_phone_number_id}")