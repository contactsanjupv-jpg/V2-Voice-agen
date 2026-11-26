"""Unique fake phone numbers for tests.

`phone_numbers.retell_phone_number_id` is UNIQUE in production and stays so. The test database accumulates every number any test
creates for the whole run, so numbers drawn at random from a 7-digit space eventually collide (a birthday collision, a few percent
of runs). A process-wide counter is collision-free and deterministic: the Nth number requested in a run is always the same.
The range starts above the fixed numbers tests hard-code (such as +15555550100 and +15551234567), which the counter never reaches.
"""
import itertools
import threading

_counter = itertools.count(1_000_000)
_lock = threading.Lock()


def unique_number() -> str:
    with _lock:
        return f"+1555{next(_counter):07d}"
