"""
Spec §11/§53: "test booking race conditions, duplicate bookings." Uses a
fake in-memory BookingProvider that can simulate a slot being taken
between the check and the write, without needing a real Google Calendar
connection.
"""
from datetime import datetime, timedelta, timezone

from app.providers.booking.booking_provider import (
    Booking,
    BookingProvider,
    LeadInfo,
    Slot,
    SlotUnavailableError,
    TimeWindow,
)
from app.services.booking_service import attempt_booking

NOON = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
SLOT = Slot(start=NOON, end=NOON + timedelta(minutes=30))
LEAD = LeadInfo(name="Test Caller", phone="+15551234567", email=None, reason="checkup")


class FakeBookingProvider(BookingProvider):
    """Configurable fake: `slot_taken_at_write_time` simulates the exact
    race the spec calls out — available at check time, gone by write time."""

    def __init__(self, available_slots: list[Slot], slot_taken_at_write_time: bool = False):
        self.available_slots = available_slots
        self.slot_taken_at_write_time = slot_taken_at_write_time
        self.create_booking_calls = 0

    def check_availability(self, window: TimeWindow) -> list[Slot]:
        return [s for s in self.available_slots if s.start >= window.start and s.end <= window.end]

    def create_booking(self, slot: Slot, lead: LeadInfo, idempotency_key: str) -> Booking:
        self.create_booking_calls += 1
        if self.slot_taken_at_write_time:
            raise SlotUnavailableError("taken between check and write")
        return Booking(external_event_id="evt_1", start=slot.start, end=slot.end)

    def cancel_booking(self, external_event_id: str) -> None:
        pass

    def reschedule_booking(self, external_event_id: str, new_slot: Slot) -> Booking:
        return Booking(external_event_id=external_event_id, start=new_slot.start, end=new_slot.end)


def test_successful_booking_when_slot_genuinely_open():
    provider = FakeBookingProvider(available_slots=[SLOT])
    result = attempt_booking(
        provider, SLOT, LEAD, idempotency_key="call_1:" + SLOT.start.isoformat(),
        fallback_window=TimeWindow(NOON, NOON + timedelta(hours=2)),
    )
    assert result.success is True
    assert result.booking.external_event_id == "evt_1"


def test_never_reports_success_before_provider_confirms():
    """The slot vanished between offer and confirm — attempt_booking must
    NOT report success, and must offer real alternatives instead of
    inventing one."""
    other_slot = Slot(start=NOON + timedelta(minutes=30), end=NOON + timedelta(minutes=60))
    provider = FakeBookingProvider(available_slots=[other_slot])  # SLOT itself no longer in the open list
    result = attempt_booking(
        provider, SLOT, LEAD, idempotency_key="call_2:" + SLOT.start.isoformat(),
        fallback_window=TimeWindow(NOON, NOON + timedelta(hours=2)),
    )
    assert result.success is False
    assert result.booking is None
    assert other_slot in result.alternative_slots
    # Must not have even attempted a provider write for a slot we already
    # know is gone.
    assert provider.create_booking_calls == 0


def test_provider_level_race_still_caught():
    """Passed the pre-check (slot looked open) but the provider itself
    rejects the write — this is the case a Google Calendar 409 maps to."""
    provider = FakeBookingProvider(available_slots=[SLOT], slot_taken_at_write_time=True)
    result = attempt_booking(
        provider, SLOT, LEAD, idempotency_key="call_3:" + SLOT.start.isoformat(),
        fallback_window=TimeWindow(NOON, NOON + timedelta(hours=2)),
    )
    assert result.success is False
    assert provider.create_booking_calls == 1  # it DID attempt, and correctly did not claim success
