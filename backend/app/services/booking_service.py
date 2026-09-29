"""
Orchestrates the check → offer → confirm → book flow described in spec
§10-11. This is the ONLY place allowed to call BookingProvider.create_booking
— nothing upstream (the Retell function-call handler) is allowed to skip
straight to booking without going through here, because this is where the
re-check-before-write race-condition guard lives.
"""
from dataclasses import dataclass

from app.providers.booking.booking_provider import (
    BookingProvider,
    LeadInfo,
    Slot,
    SlotUnavailableError,
    TimeWindow,
)


@dataclass
class BookingAttemptResult:
    success: bool
    booking: object | None
    alternative_slots: list[Slot]
    message: str


def attempt_booking(
    provider: BookingProvider,
    requested_slot: Slot,
    lead: LeadInfo,
    idempotency_key: str,
    fallback_window: TimeWindow,
    max_alternatives: int = 3,
) -> BookingAttemptResult:
    """
    Step 5-8 of the spec's booking flow:
      5. Attempt booking (provider call, not a claim of success yet)
      6. Provider confirms OR the slot was taken
      7. ONLY on provider confirmation do we report success
      8. On failure, re-check availability and return real alternatives —
         never invent a slot, never blindly retry the same one.
    """
    # Re-verify the slot is still open immediately before writing — closes
    # most of the window for a race between "offered" and "confirmed", even
    # though the provider-level check in create_booking is the actual
    # backstop (see google_calendar_provider's 409 handling).
    still_open = provider.check_availability(
        TimeWindow(start=requested_slot.start, end=requested_slot.end)
    )
    if not any(s.start == requested_slot.start and s.end == requested_slot.end for s in still_open):
        alternatives = provider.check_availability(fallback_window)[:max_alternatives]
        return BookingAttemptResult(
            success=False,
            booking=None,
            alternative_slots=alternatives,
            message="That time was just taken. Here are other times available.",
        )

    try:
        booking = provider.create_booking(requested_slot, lead, idempotency_key)
    except SlotUnavailableError:
        alternatives = provider.check_availability(fallback_window)[:max_alternatives]
        return BookingAttemptResult(
            success=False,
            booking=None,
            alternative_slots=alternatives,
            message="That time was just taken. Here are other times available.",
        )

    return BookingAttemptResult(success=True, booking=booking, alternative_slots=[], message="Booked.")
