"""
Retell phone numbers. create-phone-number both selects AND buys (area_code is a
preference); update-phone-number assigns agents; delete-phone-number releases.

UNVERIFIED against a live account (verify with the one live test in the
hand-off notes): `nickname` accepted on create and echoed by list/get;
GET /list-phone-numbers and GET /get-phone-number/{number} response field
names (phone_number, nickname, inbound_agent_id); and that
{"inbound_agent_id": null} unassigns an agent.
"""
from app.providers.phone.phone_provider import PhoneProvider, ProvisionedNumber
from app.providers.retell_client import RetellAPIError, RetellClient


def _to_number(raw: dict, country: str = "US", area_code: str | None = None) -> ProvisionedNumber:
    return ProvisionedNumber(
        provider_phone_number_id=raw["phone_number"],
        number=raw["phone_number"],
        area_code=area_code,
        country=country,
        monthly_cost_cents=raw.get("monthly_cost_cents"),
        nickname=raw.get("nickname"),
        inbound_agent_id=raw.get("inbound_agent_id"),
    )


class RetellPhoneProvider(PhoneProvider):
    def __init__(self, client: RetellClient | None = None):
        self.client = client or RetellClient()

    def purchase_number(self, country: str, area_code: str | None, nickname: str) -> ProvisionedNumber:
        payload: dict = {"country_code": country, "nickname": nickname}
        if area_code:
            payload["area_code"] = int(area_code)
        raw = self.client.request("POST", "/create-phone-number", json=payload)
        return _to_number(raw, country, area_code)

    def find_number_by_nickname(self, nickname: str) -> ProvisionedNumber | None:
        raw = self.client.request("GET", "/list-phone-numbers")
        items = raw if isinstance(raw, list) else raw.get("phone_numbers", [])
        for item in items:
            if item.get("nickname") == nickname:
                return _to_number(item)
        return None

    def get_number(self, provider_phone_number_id: str) -> ProvisionedNumber | None:
        try:
            return _to_number(self.client.request("GET", f"/get-phone-number/{provider_phone_number_id}"))
        except RetellAPIError as e:
            if e.status_code == 404:
                return None
            raise

    def assign_agent(self, provider_phone_number_id: str, provider_agent_id: str, direction: str = "inbound") -> None:
        field = "inbound_agent_id" if direction == "inbound" else "outbound_agent_id"
        self.client.request("PATCH", f"/update-phone-number/{provider_phone_number_id}", json={field: provider_agent_id})

    def unassign_agent(self, provider_phone_number_id: str) -> None:
        self.client.request("PATCH", f"/update-phone-number/{provider_phone_number_id}", json={"inbound_agent_id": None})

    def release_number(self, provider_phone_number_id: str) -> None:
        try:
            self.client.request("DELETE", f"/delete-phone-number/{provider_phone_number_id}")
        except RetellAPIError as e:
            if e.status_code != 404:
                raise
