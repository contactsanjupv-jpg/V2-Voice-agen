"""Operator control of the email quota pause. See docs/EMAIL_SAFETY.md.

    python -m app.workers.email_quota status   # is sending paused, why, until when
    python -m app.workers.email_quota clear    # resume sending now (after the provider quota was raised or has reset)
    python -m app.workers.email_quota clear --seq N   # clear only the pause entry #N that `status` showed you; refused if a newer pause exists
"""
import re
import sys

from app.core.email import BreakerStoreError, StalePauseError, check_breaker, clear_quota_pause


_SEQ = re.compile(r"[1-9][0-9]{0,17}")  # ASCII digits only, positive, fits a bigint


def _usage_error(message: str) -> int:
    print(f"error: {message}\n{__doc__}")
    return 2


def main(argv: list[str]) -> int:
    """Exact grammar: `status` | `clear` | `clear --seq N` (N a positive integer). Anything else is refused with exit code 2 and
    changes nothing: an invalid --seq is never turned into an unqualified clear."""
    if argv == [] or argv == ["status"]:
        return _status()
    if argv == ["clear"]:
        return _clear(None)
    if len(argv) == 3 and argv[0] == "clear" and argv[1] == "--seq":
        if _SEQ.fullmatch(argv[2]) is None:
            return _usage_error(f"--seq must be a positive whole number (the entry number `status` shows), got {argv[2]!r}")
        return _clear(int(argv[2]))
    return _usage_error("unrecognised or malformed arguments")


def _status() -> int:
    state = check_breaker()
    if not state.available:
        print("UNKNOWN: the shared quota state (database) cannot be read, so email sending is REFUSED until it can.")
    elif state.pause is None:
        print("Email sending is not paused.")
    elif state.pause.until is None:
        print(f"PAUSED: the provider's {state.pause.kind} quota is used up. No automatic resume: run 'clear' once the quota is available again. (entry #{state.pause.seq})")
    else:
        print(f"PAUSED: the provider's {state.pause.kind} quota is used up. Sending resumes after {state.pause.until.isoformat()} ({state.pause.source}); 'clear' resumes sooner. (entry #{state.pause.seq})")
    return 0


def _clear(expected: int | None) -> int:
    try:
        cleared = clear_quota_pause(expected)
    except StalePauseError as why:
        print(f"Nothing was cleared: a newer quota pause was recorded than the one you looked at ({why}). Run 'status' and decide again.")
        return 1
    except BreakerStoreError:
        print("Nothing was cleared: the database cannot be reached. Try again when it is.")
        return 1
    print("The quota pause was cleared; sending resumes on the next worker pass." if cleared else "There was no quota pause to clear.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
