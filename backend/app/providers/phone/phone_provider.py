from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ProvisionedNumber:
    provider_phone_number_id: str
    number: str
    area_code: str | None
    country: str
    monthly_cost_cents: int | None


class PhoneProvider(ABC):
    @abstractmethod
    def purchase_number(self, country: str, area_code: str | None) -> ProvisionedNumber:
        """
        Purchasing IS the only step — Retell's API (and most managed-number
        providers) has no separate 'browse available numbers' call before
        buying. area_code is a preference the provider tries to honor, not
        a guarantee; the actual number returned is whatever they assign.
        """
        ...

    @abstractmethod
    def assign_agent(self, provider_phone_number_id: str, provider_agent_id: str, direction: str = "inbound") -> None: ...

    @abstractmethod
    def release_number(self, provider_phone_number_id: str) -> None: ...