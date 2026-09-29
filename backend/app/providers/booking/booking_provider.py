from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass
class TimeWindow:
    start: datetime
    end: datetime


@dataclass
class Slot:
    start: datetime
    end: datetime


@dataclass
class LeadInfo:
    name: str | None
    phone: str | None
    email: str | None
    reason: str | None


@dataclass
class Booking:
    external_event_id: str
    start: datetime
    end: datetime


class SlotUnavailableError(Exception):
    """Raised when create_booking's target slot was taken between the
    check and the write — the caller must re-check availability and offer
    alternatives, never silently retry the same slot."""


class BookingProvider(ABC):
    @abstractmethod
    def check_availability(self, window: TimeWindow) -> list[Slot]: ...

    @abstractmethod
    def create_booking(self, slot: Slot, lead: LeadInfo, idempotency_key: str) -> Booking:
        """`idempotency_key` should be derived from (call_id, slot.start) by
        the caller — see app/services/booking_service.py. A retried call
        with the same key against a provider that supports idempotency
        (Google Calendar: via a deterministic event ID) must not create a
        duplicate booking."""
        ...

    @abstractmethod
    def cancel_booking(self, external_event_id: str) -> None: ...

    @abstractmethod
    def reschedule_booking(self, external_event_id: str, new_slot: Slot) -> Booking: ...
