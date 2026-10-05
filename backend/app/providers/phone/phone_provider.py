from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ProvisionedNumber:
    provider_phone_number_id: str
    number: str
    area_code: str | None
    country: str
    monthly_cost_cents: int | None
    nickname: str | None = None
    inbound_agent_id: str | None = None


class PhoneProvider(ABC):
    @abstractmethod
    def purchase_number(self, country: str, area_code: str | None, nickname: str) -> ProvisionedNumber:
        """
        Purchasing IS the only step (no browse-then-buy). `nickname` is OUR
        deterministic identifier stored on the provider resource, so after an
        ambiguous failure we can ask the provider "did this purchase happen?"
        instead of buying again.
        """

    @abstractmethod
    def find_number_by_nickname(self, nickname: str) -> ProvisionedNumber | None: ...

    @abstractmethod
    def get_number(self, provider_phone_number_id: str) -> ProvisionedNumber | None:
        """None if the provider no longer has this number."""

    @abstractmethod
    def assign_agent(self, provider_phone_number_id: str, provider_agent_id: str, direction: str = "inbound") -> None: ...

    @abstractmethod
    def unassign_agent(self, provider_phone_number_id: str) -> None: ...

    @abstractmethod
    def release_number(self, provider_phone_number_id: str) -> None:
        """Idempotent: releasing a number the provider no longer has is success."""
