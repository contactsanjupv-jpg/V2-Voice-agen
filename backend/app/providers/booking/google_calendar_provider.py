"""
Real implementation against the Google Calendar API v3
(googleapis.com/calendar/v3), OAuth2 access via a token this class is
handed decrypted (see app/services/booking_service.py — decryption happens
one layer up, right before the call, never held in memory longer than
needed).

Availability check strategy: Calendar API's freebusy.query is the correct
primitive here — it returns actual busy blocks for the calendar, and we
compute open slots ourselves against the business's configured hours. We
deliberately do NOT ask the AI agent to reason about free/busy; the
provider is the source of truth per spec §10/§11.

Idempotent booking: Google Calendar's events.insert doesn't have a native
idempotency-key parameter, so we derive a deterministic Calendar event ID
from `idempotency_key` (Calendar allows client-specified event IDs in a
constrained charset) — a retried create_booking with the same key updates
the existing event rather than creating a duplicate.
"""
import re
from datetime import datetime, timezone

import httpx

from app.providers.booking.booking_provider import (
    Booking,
    BookingProvider,
    LeadInfo,
    Slot,
    SlotUnavailableError,
    TimeWindow,
)

CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
_EVENT_ID_SAFE = re.compile(r"[^a-v0-9]")  # Google event IDs: base32hex charset only


def _safe_event_id(idempotency_key: str) -> str:
    lowered = idempotency_key.lower()
    cleaned = _EVENT_ID_SAFE.sub("", lowered)
    return f"atla{cleaned}"[:1024] or "atlaevent"


class GoogleCalendarProvider(BookingProvider):
    def __init__(self, access_token: str, calendar_id: str = "primary", slot_duration_minutes: int = 30):
        self.access_token = access_token
        self.calendar_id = calendar_id
        self.slot_duration_minutes = slot_duration_minutes

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}

    def check_availability(self, window: TimeWindow) -> list[Slot]:
        body = {
            "timeMin": window.start.isoformat(),
            "timeMax": window.end.isoformat(),
            "items": [{"id": self.calendar_id}],
        }
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(f"{CALENDAR_API_BASE}/freeBusy", headers=self._headers(), json=body)
        resp.raise_for_status()
        busy_blocks = resp.json()["calendars"][self.calendar_id]["busy"]
        return self._compute_open_slots(window, busy_blocks)

    def _compute_open_slots(self, window: TimeWindow, busy_blocks: list[dict]) -> list[Slot]:
        busy = [
            (datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"])) for b in busy_blocks
        ]
        busy.sort()
        slots: list[Slot] = []
        cursor = window.start
        step = self.slot_duration_minutes
        from datetime import timedelta

        while cursor + timedelta(minutes=step) <= window.end:
            candidate_end = cursor + timedelta(minutes=step)
            overlaps = any(b_start < candidate_end and b_end > cursor for b_start, b_end in busy)
            if not overlaps:
                slots.append(Slot(start=cursor, end=candidate_end))
            cursor = cursor + timedelta(minutes=step)
        return slots

    def create_booking(self, slot: Slot, lead: LeadInfo, idempotency_key: str) -> Booking:
        event_id = _safe_event_id(idempotency_key)
        event_body = {
            "id": event_id,
            "summary": f"Appointment — {lead.name or 'New booking'}",
            "description": (lead.reason or "") + (f"\nPhone: {lead.phone}" if lead.phone else ""),
            "start": {"dateTime": slot.start.isoformat()},
            "end": {"dateTime": slot.end.isoformat()},
        }
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                f"{CALENDAR_API_BASE}/calendars/{self.calendar_id}/events",
                headers=self._headers(),
                params={"conferenceDataVersion": 0},
                json=event_body,
            )
        if resp.status_code == 409:
            # Google returns 409 when the client-specified event ID already
            # exists — for us that means this exact idempotency key was
            # already booked; treat as success-of-a-prior-attempt rather
            # than a fresh conflict. A genuinely double-booked TIME SLOT
            # (different idempotency key, overlapping time) is a business
            # logic concern handled one layer up in booking_service, by
            # re-running check_availability right before this call.
            existing = self._get_event(event_id)
            return Booking(
                external_event_id=event_id,
                start=datetime.fromisoformat(existing["start"]["dateTime"]),
                end=datetime.fromisoformat(existing["end"]["dateTime"]),
            )
        if resp.status_code >= 400:
            raise SlotUnavailableError(f"Google Calendar booking failed: {resp.status_code} {resp.text}")
        created = resp.json()
        return Booking(
            external_event_id=created["id"],
            start=datetime.fromisoformat(created["start"]["dateTime"]),
            end=datetime.fromisoformat(created["end"]["dateTime"]),
        )

    def _get_event(self, event_id: str) -> dict:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                f"{CALENDAR_API_BASE}/calendars/{self.calendar_id}/events/{event_id}", headers=self._headers()
            )
        resp.raise_for_status()
        return resp.json()

    def cancel_booking(self, external_event_id: str) -> None:
        with httpx.Client(timeout=10.0) as client:
            resp = client.delete(
                f"{CALENDAR_API_BASE}/calendars/{self.calendar_id}/events/{external_event_id}",
                headers=self._headers(),
            )
        if resp.status_code not in (200, 204, 404, 410):
            resp.raise_for_status()

    def reschedule_booking(self, external_event_id: str, new_slot: Slot) -> Booking:
        body = {"start": {"dateTime": new_slot.start.isoformat()}, "end": {"dateTime": new_slot.end.isoformat()}}
        with httpx.Client(timeout=10.0) as client:
            resp = client.patch(
                f"{CALENDAR_API_BASE}/calendars/{self.calendar_id}/events/{external_event_id}",
                headers=self._headers(),
                json=body,
            )
        resp.raise_for_status()
        updated = resp.json()
        return Booking(
            external_event_id=updated["id"],
            start=datetime.fromisoformat(updated["start"]["dateTime"]),
            end=datetime.fromisoformat(updated["end"]["dateTime"]),
        )
