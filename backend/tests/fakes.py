"""An in-memory stand-in for Retell's phone-number API that can misbehave on demand."""
import threading
import uuid

from app.providers.phone.phone_provider import PhoneProvider, ProvisionedNumber
from app.providers.retell_client import RetellAPIError


class FakePhone(PhoneProvider):
    def __init__(self):
        self.numbers: dict[str, dict] = {}  # provider id -> {nickname, agent}
        self.purchases = 0
        self.assigns: list[tuple[str, str]] = []
        self.unassigns: list[str] = []
        self.releases: list[str] = []
        self.timeout_after_success = 0  # next N purchases succeed at the provider, then time out
        self.timeout_without_purchase = 0  # next N purchases time out and nothing was bought
        self.reject_purchases = 0  # next N purchases are definitively rejected (HTTP 400)
        self.fail_unassign = 0
        self.assign_is_noop = False
        self._lock = threading.Lock()

    def _view(self, pid):
        n = self.numbers[pid]
        return ProvisionedNumber(pid, pid, None, "US", 200, nickname=n["nickname"], inbound_agent_id=n["agent"])

    def purchase_number(self, country, area_code, nickname):
        with self._lock:
            self.purchases += 1
            if self.reject_purchases > 0:
                self.reject_purchases -= 1
                raise RetellAPIError(400, "rejected", {"error": "bad"})
            if self.timeout_without_purchase > 0:
                self.timeout_without_purchase -= 1
                raise RetellAPIError(0, "No response from Retell", ambiguous=True)
            pid = f"+1555{uuid.uuid4().int % 10_000_000:07d}"
            self.numbers[pid] = {"nickname": nickname, "agent": None}
            if self.timeout_after_success > 0:
                self.timeout_after_success -= 1
                raise RetellAPIError(0, "No response from Retell", ambiguous=True)
            return self._view(pid)

    def find_number_by_nickname(self, nickname):
        for pid, n in self.numbers.items():
            if n["nickname"] == nickname:
                return self._view(pid)
        return None

    def get_number(self, provider_phone_number_id):
        return self._view(provider_phone_number_id) if provider_phone_number_id in self.numbers else None

    def assign_agent(self, provider_phone_number_id, provider_agent_id, direction="inbound"):
        self.assigns.append((provider_phone_number_id, provider_agent_id))
        if not self.assign_is_noop:
            self.numbers[provider_phone_number_id]["agent"] = provider_agent_id

    def unassign_agent(self, provider_phone_number_id):
        if self.fail_unassign > 0:
            self.fail_unassign -= 1
            raise RetellAPIError(500, "boom")
        self.unassigns.append(provider_phone_number_id)
        if provider_phone_number_id in self.numbers:
            self.numbers[provider_phone_number_id]["agent"] = None

    def release_number(self, provider_phone_number_id):
        self.releases.append(provider_phone_number_id)
        self.numbers.pop(provider_phone_number_id, None)
