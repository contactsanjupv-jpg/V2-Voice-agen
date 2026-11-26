"""Outgoing email via SMTP (stdlib only). See docs/EMAIL_SAFETY.md and docs/PASSWORD_RESET.md.

Email bodies can contain secrets (password-reset links), so they are NEVER logged. Only a masked recipient, the subject and a
short failure class are.

Three safety properties live here, in the one place that can open an SMTP connection:

1. TEST MODE NEVER SENDS. When ENV=test, or when pytest is running (even with ENV=development, which is what a bare local run
   uses), `send_email` refuses to open any SMTP connection, whatever credentials are configured. It does not matter what
   `.env` contains. Tests that want to see what would have been sent install a catcher with `install_test_transport`.
2. FAILURES ARE CLASSIFIED. A permanent failure (bad recipient, rejected credentials) must not be retried; a quota failure must
   stop ALL sending until the quota resets or an operator intervenes; only a transient failure is worth retrying.
3. A QUOTA FAILURE TRIPS A SHARED, DURABLE BREAKER (PostgreSQL) so every process stops hammering the provider at once. Daily, monthly and
   unknown allowances are told apart; ordinary rate limiting is not a quota. Only a reset time the server itself states is trusted:
   without one, a monthly or unknown quota pauses sending until an OPERATOR resumes it. The pause is stored in PostgreSQL, so it
   survives restarts and Redis outages. If it cannot be read, sending is REFUSED (fail closed), never allowed on a guess.
"""
import enum
import logging
import os
import re
import smtplib
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Callable

from sqlalchemy import BigInteger, text

from app.config import get_settings

logger = logging.getLogger("atla.email")


def mask_address(address: str) -> str:
    """'jane.doe@example.com' -> 'j***@example.com' (enough to correlate a log line, not to harvest addresses)."""
    local, _, domain = address.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


# ---------------------------------------------------------------- outcomes

class EmailOutcome(str, enum.Enum):
    SENT = "sent"  # an SMTP server accepted the message
    TRANSIENT = "transient"  # worth retrying later (timeout, 4xx, rate limiting, connection trouble, unknown)
    PERMANENT = "permanent"  # retrying cannot help (rejected recipient or sender, rejected credentials)
    QUOTA = "quota"  # the provider's daily/monthly allowance is used up: stop sending (see `quota_kind`, `retry_after`)
    HELD = "held"  # refused WITHOUT contacting the provider because the quota breaker cannot be read (database unreachable)
    NOT_CONFIGURED = "not_configured"  # SMTP_HOST is empty
    BLOCKED = "blocked"  # test mode: sending is refused by design


@dataclass(frozen=True)
class EmailResult:
    outcome: EmailOutcome
    detail: str = ""  # short and safe: an exception class and a numeric SMTP code. Never an address or message text.
    retry_after: datetime | None = None  # QUOTA only: when sending may resume. None = an operator must resume it.
    quota_kind: str | None = None  # QUOTA only: "daily" | "monthly" | "unknown"

    @property
    def ok(self) -> bool:
        return self.outcome is EmailOutcome.SENT


# ---------------------------------------------------------------- test mode (fail closed)

_force_test_mode = False
_test_transport: Callable[[str, str, str], bool] | None = None
blocked_in_test_mode: list[tuple[str, str]] = []  # (masked recipient, subject) of every send test mode refused


def force_test_mode() -> None:
    """Called by the test suite's conftest at import time. Irreversible for the life of the process, on purpose."""
    global _force_test_mode
    _force_test_mode = True


def in_test_mode() -> bool:
    return _force_test_mode or bool(os.environ.get("PYTEST_CURRENT_TEST")) or get_settings().ENV == "test"


def install_test_transport(transport: Callable[[str, str, str], bool] | None):
    """A catcher for tests: it receives (to, subject, body) instead of any SMTP server and returns whether to pretend the message
    was accepted. It is consulted ONLY in test mode, so installing one can never make a production process skip SMTP, and
    nothing here can make a test reach a real server. Returns the previous transport so it can be restored."""
    global _test_transport
    previous, _test_transport = _test_transport, transport
    return previous


def _test_mode_send(to: str, subject: str, body: str) -> EmailResult:
    if _test_transport is not None:
        accepted = _test_transport(to, subject, body)
        return EmailResult(EmailOutcome.SENT if accepted else EmailOutcome.TRANSIENT, "test catcher")
    blocked_in_test_mode.append((mask_address(to), subject))
    logger.warning("Test mode: email to %s (%s) was NOT sent; test mode never contacts an SMTP server", mask_address(to), subject)
    return EmailResult(EmailOutcome.BLOCKED, "test mode")


# ---------------------------------------------------------------- quota breaker (durable, shared by every process via PostgreSQL)
# The pause is the newest of two audit_logs events ("email.quota_pause" / "email.quota_clear"). It is not in Redis: Redis can be down or
# lose its keys, and a pause that vanishes would let every process resume sending into an exhausted quota. audit_logs is used (it is
# append-only, needs no migration, and records every operator clear) rather than a new table, so this patch cannot create a second
# Alembic head next to other migrations.
#
# ORDER. "Newest" is a per-event sequence number (`seq` in the event's metadata), NOT a timestamp. Every writer takes one PostgreSQL
# advisory lock, reads the latest seq and writes seq + 1 in the same transaction, so events are totally ordered by commit order: no two
# share a seq, a slow or skewed clock cannot reorder them, and a clear can never land "before" a pause that was committed first.
#
# WRITABLE BEFORE EVERY SEND. Reading the breaker is not enough: a database that answers reads but refuses writes (read-only failover,
# disk full, lock or statement timeouts) cannot record a quota failure, so a sender that reached the provider could not stop the others.
# So every send first does a real durable write (a heartbeat row updated in place, in the same transaction that reads the pause).
# If that write fails, nothing is sent. The provider is contacted only by a process that has just proved it can record what happens next.

PAUSE_ACTION = "email.quota_pause"
CLEAR_ACTION = "email.quota_clear"
PROBE_ACTION = "email.quota_probe"  # one heartbeat row, updated in place (never grows)
_AUDIT_TARGET = "email_provider"
_LOCK_NAME = "atla:email-quota-breaker"
_QUOTA_MARGIN = timedelta(minutes=5)  # after a daily reset we assume (UTC midnight is a convention, NOT verified for any provider)
_QUOTA_MIN_PAUSE = timedelta(minutes=30)
_MAX_SERVER_RESET = timedelta(days=45)  # a server-stated reset further away than this is not believed
_PERSIST_ATTEMPTS = 3  # tries to record a pause before holding it locally
_PERSIST_DELAY = 0.25  # seconds between tries


class BreakerStoreError(RuntimeError):
    """The shared quota state could not be read or written."""


class StalePauseError(RuntimeError):
    """A clear was refused because a newer pause than the one the operator looked at has been recorded."""


@dataclass(frozen=True)
class QuotaPause:
    kind: str  # "daily" | "monthly" | "unknown"
    until: datetime | None  # None = an operator must resume sending (python -m app.workers.email_quota clear)
    source: str  # "server" (the provider stated the reset time) | "convention" (daily: next UTC midnight) | "operator-required"
    seq: int = field(default=0, compare=False)  # position in the shared event order (0: not recorded yet)

    @property
    def operator_required(self) -> bool:
        return self.until is None


@dataclass(frozen=True)
class BreakerState:
    available: bool  # False: the shared state could not be read AND written, so nobody may assume sending is safe
    pause: QuotaPause | None = None


# Set only when the database could not record a pause. It protects THIS process, and is published to the database (then dropped) the
# first time the database is reachable again. Other processes are protected by the writability gate, not by this.
_local_pause: QuotaPause | None = None
# Guards EVERY read and write of _local_pause (nothing else may touch it; a test enforces that). It is held only for the in-memory
# step itself, never across database I/O. A "compare, then clear" must be ONE step under this lock: two separate steps would let a
# newer pause from another thread arrive in between and be erased by a clear that never saw it.
_local_lock = threading.Lock()


def _get_local() -> QuotaPause | None:
    with _local_lock:
        return _local_pause


def _compare(a: QuotaPause, b: QuotaPause) -> int:
    """THE merge rule. 1 if `a` keeps sending stopped longer than `b`, -1 if shorter, 0 if equal: a pause with no resume time
    (an operator must resume it) is stricter than any pause with one; of two with resume times, the later is stricter."""
    if a.until is None and b.until is None:
        return 0
    if a.until is None:
        return 1
    if b.until is None:
        return -1
    return (a.until > b.until) - (a.until < b.until)


def _stricter(new: QuotaPause, current: QuotaPause) -> QuotaPause:
    """Of two pauses, the stricter one; on a tie, `new`."""
    return current if _compare(current, new) > 0 else new


def _merge_local(pause: QuotaPause) -> QuotaPause:
    """Records an emergency pause in this process. A second one never weakens the first."""
    global _local_pause
    with _local_lock:
        _local_pause = pause if _local_pause is None else _stricter(pause, _local_pause)
        return _local_pause


def _replace_local(pause: QuotaPause | None) -> None:
    """Sets (or, with None, drops) the local pause under the lock. For tests and fixtures: nothing may assign the variable directly."""
    global _local_pause
    with _local_lock:
        _local_pause = pause


def _clear_local_if(expected: QuotaPause | None) -> bool:
    """Atomic: clears the local pause only if it is still exactly `expected` (the one that was inspected). A newer pause that arrived
    since is a different object and is left in place."""
    global _local_pause
    with _local_lock:
        if expected is not None and _local_pause is expected:
            _local_pause = None
            return True
        return False


def _seq_of(row) -> int:
    return int(((row.audit_metadata or {}).get("seq")) or 0)


def _latest(db):
    from app.db.models.platform import AuditLog

    seq = AuditLog.audit_metadata["seq"].astext.cast(BigInteger)
    return (
        db.query(AuditLog)
        .filter(AuditLog.action.in_((PAUSE_ACTION, CLEAR_ACTION)))
        .order_by(seq.desc().nullslast(), AuditLog.created_at.desc())  # created_at only orders legacy rows that have no seq
        .first()
    )


def _pause_of(row) -> QuotaPause | None:
    if row is None or row.action == CLEAR_ACTION:
        return None
    meta = row.audit_metadata or {}
    until = datetime.fromisoformat(meta["until"]) if meta.get("until") else None
    return QuotaPause(str(meta.get("kind") or "unknown"), until, str(meta.get("source") or "operator-required"), _seq_of(row))


def _breaker_session():
    """The session for every quota-breaker transaction. The design REQUIRES READ COMMITTED: after waiting for the advisory lock, the next
    statement must take a fresh snapshot and see what the previous lock holder committed. Under REPEATABLE READ or SERIALIZABLE the snapshot
    is taken by the first statement (the lock request itself, BEFORE the wait), so the read after the wait would be stale. The level is
    therefore set explicitly here, whatever the engine default is, and _lock verifies it."""
    from app.db.base import SessionLocal

    db = SessionLocal()
    db.connection(execution_options={"isolation_level": "READ COMMITTED"})
    return db


def _lock(db) -> None:
    """The first statement of every breaker transaction: wait for the writer lock (released at commit/rollback). Then verify the assumption
    the rest of the protocol rests on, so a different isolation level fails closed (BreakerStoreError) instead of silently reading stale state."""
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:name))"), {"name": _LOCK_NAME})
    level = db.execute(text("SELECT current_setting('transaction_isolation')")).scalar()
    if level != "read committed":
        raise RuntimeError(f"quota breaker needs READ COMMITTED, transaction is {level}")


def _append_event(db, action: str, metadata: dict, expect_seq: int | None = None, cover=None, now: datetime | None = None) -> int:
    """Adds one event to the shared order, inside the caller's transaction (which commits). Takes the writer lock FIRST, then reads
    the newest event, so every decision below is made on the state as it is under the lock, never on an earlier snapshot.
      expect_seq: refuse (StalePauseError) unless the newest event is exactly that one.
      cover:      called with the pause in force right now (or None); if it returns True nothing is written and the seq of the newest
                  event is returned. This is how a pause is recorded only when it is stricter than what is already in force."""
    from app.db.models.platform import AuditLog

    _lock(db)
    latest = _latest(db)
    newest = _seq_of(latest) if latest is not None else 0
    if expect_seq is not None and newest != expect_seq:
        raise StalePauseError(f"newest quota event is #{newest}, not #{expect_seq}")
    if cover is not None and cover(_active(_pause_of(latest), now or datetime.now(timezone.utc))):
        return newest
    seq = newest + 1
    db.add(AuditLog(organization_id=None, actor_user_id=None, action=action, target_type=_AUDIT_TARGET, target_id="smtp", audit_metadata={**metadata, "seq": seq}))
    return seq


def _store_read() -> QuotaPause | None:
    """The shared pause (possibly already expired), or None. Raises BreakerStoreError if it cannot be read."""

    try:
        db = _breaker_session()
        try:
            return _pause_of(_latest(db))
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 - unreadable or corrupt: unknown, therefore not safe
        raise BreakerStoreError(type(exc).__name__) from exc


def _store_write(action: str, metadata: dict, expect_seq: int | None = None, cover=None, now: datetime | None = None) -> int:

    try:
        db = _breaker_session()
        try:
            seq = _append_event(db, action, metadata, expect_seq, cover, now)
            db.commit()
            return seq
        finally:
            db.close()
    except StalePauseError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BreakerStoreError(type(exc).__name__) from exc


def _gate_store(now: datetime) -> QuotaPause | None:
    """The check every send passes: in ONE transaction, under the writer lock, read the pause and (when sending is allowed) write the
    heartbeat. Raises BreakerStoreError if the state cannot be read or the database refuses the write."""
    from app.db.models.platform import AuditLog

    try:
        db = _breaker_session()
        try:
            _lock(db)
            pause = _active(_pause_of(_latest(db)), now)
            if pause is None:
                beat = {"at": now.isoformat()}
                touched = db.query(AuditLog).filter(AuditLog.action == PROBE_ACTION).update({"audit_metadata": beat, "updated_at": now}, synchronize_session=False)
                if touched == 0:
                    db.add(AuditLog(organization_id=None, actor_user_id=None, action=PROBE_ACTION, target_type=_AUDIT_TARGET, target_id="smtp", audit_metadata=beat))
            db.commit()  # a read-only or full database fails HERE, before the provider is ever contacted
            return pause
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        raise BreakerStoreError(type(exc).__name__) from exc


def _record_pause(candidate: QuotaPause, now: datetime) -> QuotaPause:
    """Records `candidate` as the newest event ONLY IF it is stricter than the pause in force, decided under the writer lock on the
    freshest state (a stale snapshot can therefore never publish over a newer event, and a weaker pause can never become the
    newest event over a stricter active one). Returns the pause in force afterwards: the candidate (with its seq), or the existing
    pause that already covers it."""
    covering: list[QuotaPause] = []

    def covered(active: QuotaPause | None) -> bool:
        if active is not None and _compare(active, candidate) >= 0:
            covering.append(active)
            return True
        return False

    meta = {"kind": candidate.kind, "until": candidate.until.isoformat() if candidate.until else None, "source": candidate.source, "set_at": now.isoformat()}
    seq = _store_write(PAUSE_ACTION, meta, cover=covered, now=now)
    return covering[0] if covering else replace(candidate, seq=seq)


def _active(pause: QuotaPause | None, now: datetime) -> QuotaPause | None:
    if pause is None:
        return None
    return pause if pause.until is None or pause.until > now else None


def _reconcile(stored: QuotaPause | None, now: datetime) -> QuotaPause | None:
    """The effective pause: the STRICTER of the recorded pause and this process's local one (see _compare).

    A local pause exists only because the database could not record it. Here it is recorded with _record_pause, which decides under the
    writer lock on the freshest state: it becomes the newest event if it is stricter than what is in force, or it is found to be already
    covered by an equal or stricter pause. Either way the durable state is then at least as strict as the local pause, and only then is
    the local pause dropped (and only if it is still exactly the one that was handled: a newer local pause stays). `stored` is a snapshot
    taken earlier and is used only if the database cannot be reached now; it never decides a write."""
    local = _active(_get_local(), now)
    if local is None:
        return stored
    try:
        effective = _record_pause(local, now)
    except BreakerStoreError:
        return local if stored is None else _stricter(local, stored)  # cannot record it now: it still applies, and is retried on the next check
    _clear_local_if(local)
    return effective


def check_breaker(now: datetime | None = None, *, for_sending: bool = False) -> BreakerState:
    """Is sending paused for the provider quota? `available=False` means "do not send" (fail closed): the shared state could not be
    read, or (for_sending=True, which every real send uses) the database would not accept a write, so a quota failure could not be
    recorded. for_sending=False only reads (the worker uses it to look before a pass). When paused, `pause` is the stricter of the
    recorded and the local pause."""
    now = now or datetime.now(timezone.utc)
    try:
        stored = _gate_store(now) if for_sending else _active(_store_read(), now)
    except BreakerStoreError:
        local = _active(_get_local(), now)
        return BreakerState(True, local) if local is not None else BreakerState(False)
    return BreakerState(True, _reconcile(stored, now))


def pause_for_quota(kind: str = "unknown", reset_at: datetime | None = None, now: datetime | None = None) -> QuotaPause:
    """Stops all sending, durably. Resume time, in order of trust:
      1. `reset_at`, when the SERVER stated it (a timezone-aware time in the future, at most 45 days away);
      2. a DAILY quota with no stated time: the next UTC midnight + 5 min, at least 30 min (a convention, not verified for any
         provider; if the quota is still exhausted the next send simply pauses again);
      3. a MONTHLY or UNKNOWN quota with no stated time: no automatic resume. An operator must run
         `python -m app.workers.email_quota clear`. A monthly reset is never guessed."""
    now = now or datetime.now(timezone.utc)
    if reset_at is not None and now < reset_at <= now + _MAX_SERVER_RESET:
        pause = QuotaPause(kind, reset_at, "server")
    elif kind == "daily":
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        pause = QuotaPause(kind, max(midnight + _QUOTA_MARGIN, now + _QUOTA_MIN_PAUSE), "convention")
    else:
        pause = QuotaPause(kind, None, "operator-required")
    # A stricter pause this process is already holding is recorded FIRST, so the durable state never shows the weaker pause as the newest.
    if _get_local() is not None:
        _reconcile(None, now)
    recorded = False
    for attempt in range(_PERSIST_ATTEMPTS):
        try:
            pause = _record_pause(pause, now)  # the pause in force afterwards: this one, or an equal/stricter one that was already recorded
            recorded = True
            break
        except BreakerStoreError:
            if attempt + 1 < _PERSIST_ATTEMPTS:
                time.sleep(_PERSIST_DELAY)
    if recorded:
        pause = _reconcile(pause, now) or pause  # a stricter local pause whose earlier publication failed
    else:
        pause = _merge_local(pause)  # held (never weakening an existing local pause); recorded by the next check once the database accepts writes
        logger.error("Could not record the email quota pause in the database; this process holds it, and every other process is held while the database refuses writes")
    when = "until an operator resumes it" if pause.until is None else f"until {pause.until.strftime('%Y-%m-%d %H:%M')} UTC"
    logger.error("Email provider %s quota is exhausted: ALL email sending is paused %s (operator override: python -m app.workers.email_quota clear)", pause.kind, when)
    return pause


def clear_quota_pause(expected_seq: int | None = None) -> bool:
    """Operator action: resume sending now. True if a pause was in place.

    SEQUENCE-QUALIFIED (`expected_seq=N`, what `clear --seq N` does): clears ONLY the recorded pause #N the operator looked at,
    compare-and-clear under the writer lock. If a newer pause has been recorded, StalePauseError is raised and nothing changes. A
    pause this process holds only in memory (not recorded yet, seq 0) is a different pause that no entry number can name, so it is
    NEVER discarded by a qualified clear; it stays in force and is published to the database by the next check.

    UNQUALIFIED (`expected_seq=None`): the operator's explicit "resume everything now". It clears the recorded pause and also the
    local pause this process was holding when the clear started. A local pause set after that moment (a send still in flight whose
    quota reply arrives during the clear) is a newer pause the operator never saw, so it is kept.

    In every failure (database unreadable or unwritable: BreakerStoreError; stale: StalePauseError) nothing is cleared anywhere and
    the local pause is kept."""
    local = _get_local()
    stored = _active(_store_read(), datetime.now(timezone.utc))  # may raise BreakerStoreError: nothing has been touched yet
    if stored is None:
        if local is None:
            return False
        if expected_seq is not None and expected_seq != local.seq:
            raise StalePauseError(f"the only pause here is this process's unrecorded one (#{local.seq}), not #{expected_seq}")
        _clear_local_if(local)  # nothing is recorded in the database: clearing the local pause IS the clear (atomic compare-and-clear)
        return True
    if expected_seq is not None and stored.seq != expected_seq:
        raise StalePauseError(f"the pause is now #{stored.seq}, not #{expected_seq}")
    _store_write(CLEAR_ACTION, {"by": "operator", "clears_seq": stored.seq, "qualified": expected_seq is not None}, expect_seq=stored.seq)  # failure: local kept
    if expected_seq is None:
        _clear_local_if(local)  # an unqualified clear also resumes what this process was holding; a qualified one never touches it
    return True


# ---------------------------------------------------------------- classifying SMTP failures
# What an SMTP provider says when its allowance is used up is NOT standardised, and the exact text Resend sends over SMTP has not
# been verified here (no live call is made by this project's tests). Classification therefore works on plain wording and is
# deliberately conservative: an unrecognised refusal is TRANSIENT, which backs off and is bounded, never an indefinite pause.

_ALLOWANCE = ("quota", "limit", "allowance", "exhausted")
_MONTHLY = ("monthly", "per month", "this month", "for the month")
_DAILY = ("daily", "per day", "for the day", "for today", "today")
_ISO_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})")


def _text(value) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) else str(value or "")


def _normal(message: str) -> str:
    return re.sub(r"[_\-]+", " ", message.lower())


def quota_kind(message: str) -> str | None:
    """"monthly" | "daily" | "unknown" when the text says an ALLOWANCE is used up; None for anything else, including plain rate
    limiting ("rate limit exceeded", "too many requests", "slow down"), which is temporary and must not pause all sending."""
    low = _normal(message)
    has_allowance = any(word in low for word in _ALLOWANCE)
    if has_allowance and any(word in low for word in _MONTHLY):
        return "monthly"  # checked first: when both are mentioned the longer wait is the safe reading
    if has_allowance and any(word in low for word in _DAILY):
        return "daily"
    if "quota" in low or "allowance" in low:
        return "unknown"
    return None


def _is_rate_limit(code: int | None, message: str) -> bool:
    low = _normal(message)
    return code == 429 or "rate limit" in low or "too many requests" in low or "throttl" in low or "slow down" in low


def server_stated_reset(message: str) -> datetime | None:
    """A reset time written in the server's own text, only when it carries an explicit timezone. Anything else is not trusted."""
    for match in _ISO_TIME.finditer(message):
        try:
            when = datetime.fromisoformat(match.group(0).replace("Z", "+00:00").replace(" ", "T"))
        except ValueError:
            continue
        if when.tzinfo is not None:
            return when.astimezone(timezone.utc)
    return None


def _quota_result(name: str, code, message: str) -> EmailResult | None:
    kind = quota_kind(message)
    if kind is None:
        return None
    return EmailResult(EmailOutcome.QUOTA, f"{name} {code}".strip() + f" {kind} quota", retry_after=server_stated_reset(message), quota_kind=kind)


def classify_smtp_error(exc: BaseException) -> EmailResult:
    """Turns an exception from smtplib into an outcome. Only the exception CLASS, a numeric code and a label are kept in
    `detail`: the server's text can echo the recipient or the message, so it is read for classification and then dropped."""
    name = type(exc).__name__
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return EmailResult(EmailOutcome.PERMANENT, f"{name} {exc.smtp_code}")  # wrong credentials: retrying can lock the account
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        replies = [(int(code), _text(msg)) for code, msg in exc.recipients.values()]
        for _code, msg in replies:
            quota = _quota_result(name, "", msg)
            if quota is not None:
                return quota
        if any(_is_rate_limit(code, msg) for code, msg in replies):
            return EmailResult(EmailOutcome.TRANSIENT, f"{name} rate limited")
        if replies and all(500 <= code < 600 for code, _msg in replies):
            return EmailResult(EmailOutcome.PERMANENT, f"{name}")
        return EmailResult(EmailOutcome.TRANSIENT, f"{name}")
    if isinstance(exc, smtplib.SMTPResponseException):  # sender refused, data error, connect error, ...
        code, message = int(exc.smtp_code), _text(exc.smtp_error)
        quota = _quota_result(name, code, message)
        if quota is not None:
            return quota
        if _is_rate_limit(code, message):
            return EmailResult(EmailOutcome.TRANSIENT, f"{name} {code} rate limited")  # temporary: back off, do not pause everything
        if 500 <= code < 600:
            return EmailResult(EmailOutcome.PERMANENT, f"{name} {code}")
        return EmailResult(EmailOutcome.TRANSIENT, f"{name} {code}")
    # Timeouts, resets, DNS and TLS trouble, and anything unrecognised: retry with backoff. Retries are bounded by the caller.
    return EmailResult(EmailOutcome.TRANSIENT, name)


# ---------------------------------------------------------------- the transport (the only code that opens a connection)

def _transport(settings, to: str, subject: str, body: str) -> EmailResult:
    """Opens an SMTP connection and sends. Never called in test mode (see send_email_result)."""
    who = mask_address(to)
    if not settings.SMTP_HOST:
        # No body here on purpose: a reset link is a credential.
        if settings.is_production:
            logger.error("SMTP is not configured: email to %s (%s) was NOT sent", who, subject)
        else:
            logger.warning(
                "SMTP is not configured: email to %s (%s) was NOT sent. For local development run a catcher "
                "such as Mailpit and set SMTP_HOST=localhost SMTP_PORT=1025 SMTP_STARTTLS=false.",
                who,
                subject,
            )
        return EmailResult(EmailOutcome.NOT_CONFIGURED)
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = settings.SMTP_FROM, to, subject
    msg.set_content(body)
    try:
        if settings.SMTP_USE_SSL or settings.SMTP_PORT == 465:
            # Implicit TLS (port 465). Plain SMTP + STARTTLS against this port just hangs until the timeout.
            with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as smtp:
                if settings.SMTP_USERNAME:
                    smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as smtp:
                if settings.SMTP_STARTTLS:
                    smtp.starttls()
                if settings.SMTP_USERNAME:
                    smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
                smtp.send_message(msg)
        return EmailResult(EmailOutcome.SENT)
    except Exception as exc:  # noqa: BLE001
        result = classify_smtp_error(exc)
        # Class name and numeric code only: exception text from some servers echoes the recipient or message.
        logger.error("Sending email to %s (%s) failed: %s [%s]", who, subject, result.detail, result.outcome.value)
        return result


# ---------------------------------------------------------------- public API

def send_email_result(to: str, subject: str, body: str) -> EmailResult:
    """Sends one email and says what happened. Never raises: callers must not reveal (via errors or timing) whether an address
    has an account."""
    if in_test_mode():
        return _test_mode_send(to, subject, body)
    state = check_breaker(for_sending=True)
    if not state.available:
        # The database cannot say whether a quota failure was recorded. Sending on a guess could hammer an exhausted provider from every
        # process, so nothing is sent. Callers keep the message and try later (the owner-notification outbox does).
        logger.error("Email to %s (%s) was NOT attempted: the quota breaker (database) cannot be read or written", mask_address(to), subject)
        return EmailResult(EmailOutcome.HELD, "quota breaker unavailable")
    if state.pause is not None:
        when = "until an operator resumes it" if state.pause.until is None else f"until {state.pause.until.strftime('%Y-%m-%d %H:%M')} UTC"
        logger.warning("Email to %s (%s) was NOT attempted: sending is paused for the provider's %s quota %s", mask_address(to), subject, state.pause.kind, when)
        return EmailResult(EmailOutcome.QUOTA, "paused", retry_after=state.pause.until, quota_kind=state.pause.kind)
    result = _transport(get_settings(), to, subject, body)
    if result.outcome is EmailOutcome.QUOTA:
        pause = pause_for_quota(result.quota_kind or "unknown", result.retry_after)
        result = EmailResult(EmailOutcome.QUOTA, result.detail, retry_after=pause.until, quota_kind=pause.kind)
    return result


def send_email(to: str, subject: str, body: str) -> bool:
    """Returns True only if an SMTP server accepted the message. Never raises."""
    return send_email_result(to, subject, body).ok
