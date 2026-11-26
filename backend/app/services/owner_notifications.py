"""
The email outbox machinery for owner notifications, and the single entry point (`enqueue`) services call when something
happened that the business should know about.

NOTIFICATION POLICY (docs/NOTIFICATIONS.md): email is permitted ONLY for password recovery and security alerts, which the
account endpoints send directly. Everything below is operational and is therefore IN-APP ONLY:

  lead             a call captured a new lead (real calls only, never a test call)
  service_paused   the receptionist was switched off: credits spent, payment failed or subscription ended
  low_credits      the plan's minutes are running low (calls are still being answered)
  credits_empty    the minutes are used up; the receptionist pauses itself once the overage grace is also spent
  payment_failed   Paddle reports the subscription past due

enqueue() records the in-app notification (services/notifications.py) in the caller's transaction. EMAIL_KINDS is empty, so it
queues NO email, and an email problem can never touch the in-app notification (they are independent: the in-app row is written
first, in its own savepoint, and never reads any mail-server or provider state).

The outbox machinery below is kept, unchanged and tested, for a future security-alert email: that would be one more entry in
EMAIL_KINDS plus a renderer. Until then, send_due_notifications() only drains rows queued BEFORE this policy (turning each into
its in-app notification, then skipping the email), so no pre-policy email is sent after deploy either.

How the outbox stays reliable (when a kind is email-eligible):
  * enqueue() writes one row in the SAME transaction as the thing that caused it. UNIQUE(organization_id, kind, dedupe_key)
    makes a repeated cause (webhook redelivery, sweeper retry, reconciler) a no-op.
  * send_due_notifications() is run by the worker. It builds the message from LIVE data at send time, skips a notification
    that is no longer true, and retries a failed send with backoff. Webhooks and the call pipeline never wait for SMTP.
  * Delivery is at-least-once: a crash between the SMTP hand-off and the commit can send one email twice.
"""
import hashlib
import hmac
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import Text, cast, func, literal_column, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.email import EmailOutcome, EmailResult, check_breaker, send_email_result
from app.db.models.billing import Subscription
from app.db.models.crm import Lead
from app.db.models.notifications import OwnerNotification
from app.db.models.tenancy import OrganizationMember, OrgRole, User
from app.db.models.telephony import PhoneNumber, PhoneNumberStatus
from app.db.models.voice_agent import Agent, AgentStatus
from app.services.billing_state import has_active_subscription
from app.services.business_time import format_local, organization_timezone
from app.services import notifications
from app.services.credits import credit_summary

logger = logging.getLogger("atla.owner_notifications")

KIND_LEAD = "lead"
KIND_SERVICE_PAUSED = "service_paused"
KIND_LOW_CREDITS = "low_credits"
KIND_CREDITS_EMPTY = "credits_empty"
KIND_PAYMENT_FAILED = "payment_failed"

# Why the receptionist was paused (payload["reason"] of a service_paused notification).
REASON_CREDITS = "credits_exhausted"
REASON_PAST_DUE = "payment_past_due"
REASON_LAPSED = "subscription_lapsed"

# Kinds that may be EMAILED. Empty by policy: password recovery and security alerts are sent by the account endpoints, and every
# operational alert is in-app only. Adding a kind here is a policy change (see tests/test_notification_policy.py).
EMAIL_KINDS: frozenset[str] = frozenset()

_BACKOFF_BASE_SECONDS = 60
_BACKOFF_CAP_SECONDS = 3600
# A pass stops after this many retry-worthy failures in a row: a provider that is down or refusing us is not hammered with
# every pending row, and the remaining rows simply wait for the next pass.
_MAX_CONSECUTIVE_FAILURES = 5
_OPERATOR_RECHECK = timedelta(hours=1)  # quota with no automatic resume: the row waits; the worker pass touches nothing while paused
_HELD_RECHECK = timedelta(minutes=5)  # the quota breaker (Redis) could not be read
_LEASE = timedelta(minutes=10)  # a row being sent is not due for anyone else until this has passed


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- enqueue

def enqueue(db: Session, organization_id: uuid.UUID, kind: str, dedupe_key: str, payload: dict | None = None) -> bool:
    """Record that the business must be told. True if this call created the in-app notification.

    The in-app notification is written FIRST, in its own savepoint, whatever the email settings are: it never depends on the mail
    server, the mail provider or OWNER_NOTIFICATIONS_ENABLED. Then, only for an email-eligible kind (none today), the email outbox row, in a second
    savepoint, so a failure there cannot undo or block the in-app one.

    Never raises and never poisons the caller's transaction: a notification problem must not fail a webhook, a call ingest or an
    entitlement pass. Both rows commit or roll back with the caller."""
    created = notifications.create(db, organization_id, kind, dedupe_key, payload)
    if kind in EMAIL_KINDS and get_settings().OWNER_NOTIFICATIONS_ENABLED:
        try:
            with db.begin_nested():
                db.add(
                    OwnerNotification(
                        organization_id=organization_id,
                        kind=kind,
                        dedupe_key=str(dedupe_key)[:255],
                        payload=payload or {},
                        status="pending",
                        attempts=0,
                    )
                )
                db.flush()
        except IntegrityError:
            pass  # already queued: the same cause seen again
        except Exception:  # noqa: BLE001
            logger.exception("Could not queue an owner email (kind=%s)", kind)
    return created


def pause_reason(db: Session, organization_id: uuid.UUID) -> str:
    """Why an org that is being suspended right now is being suspended (mirrors services/entitlement.py)."""
    from app.services.billing_state import current_subscription

    if has_active_subscription(db, organization_id):
        return REASON_CREDITS
    sub = current_subscription(db, organization_id)
    return REASON_PAST_DUE if sub is not None and sub.status == "past_due" else REASON_LAPSED


def has_live_service(db: Session, organization_id: uuid.UUID) -> bool:
    """True while the receptionist could still be answering calls: an active agent or an active number."""
    return (
        db.query(Agent.id).filter(Agent.organization_id == organization_id, Agent.status == AgentStatus.active).first()
        is not None
        or db.query(PhoneNumber.id)
        .filter(PhoneNumber.organization_id == organization_id, PhoneNumber.status == PhoneNumberStatus.active)
        .first()
        is not None
    )


_last_credit_pass: float | None = None  # monotonic time of the last pass; None = never (a fresh worker checks at once)


def credit_alert_pass(db: Session) -> dict | None:
    """Worker entry: at most one credit-level check per CREDIT_ALERT_INTERVAL_SECONDS (it reads whole ledgers)."""
    global _last_credit_pass
    interval = get_settings().CREDIT_ALERT_INTERVAL_SECONDS
    if _last_credit_pass is not None and time.monotonic() - _last_credit_pass < interval:
        return None
    _last_credit_pass = time.monotonic()
    return notify_credit_levels(db)


def notify_credit_levels(db: Session, now: datetime | None = None, organization_ids: set[uuid.UUID] | None = None) -> dict:
    """Queue a low / empty warning for every PAYING organization whose receptionist is live.

    One email per kind per billing period (the key is the period start), so a customer is warned once, not on every
    pass. Organizations that are not subscribed are skipped: they get the service_paused email instead."""
    now = now or _now()
    org_ids = {r[0] for r in db.query(Agent.organization_id).filter(Agent.status == AgentStatus.active).all()} | {
        r[0] for r in db.query(PhoneNumber.organization_id).filter(PhoneNumber.status == PhoneNumberStatus.active).all()
    }
    if organization_ids is not None:
        org_ids &= organization_ids
    result = {"checked": 0, "queued": 0, "errors": 0}
    for org_id in org_ids:
        try:
            if not has_active_subscription(db, org_id):
                continue
            result["checked"] += 1
            s = credit_summary(db, org_id, now)
            if s.status == "ok":
                continue
            kind = KIND_CREDITS_EMPTY if s.status == "empty" else KIND_LOW_CREDITS
            key = s.period_start.isoformat() if s.period_start else "no-period"
            if enqueue(db, org_id, kind, key, {"period_start": key}):
                result["queued"] += 1
            db.commit()
        except Exception:  # noqa: BLE001 - one organization must never stop the others
            db.rollback()
            result["errors"] += 1
            logger.exception("Credit-level check failed [org=%s]", org_id)
    return result


# ---------------------------------------------------------------- rendering

def _one_line(value, limit: int) -> str:
    """Caller-supplied text made safe for an email HEADER or a one-line field: no newlines (a newline in a header
    makes the mail library refuse the whole message, which would block every retry), no control characters."""
    text = "".join(ch for ch in str(value or "") if ch.isprintable() or ch.isspace())
    return " ".join(text.split())[:limit]


def _block(value, limit: int) -> str:
    """Caller-supplied text for the BODY: control characters removed, length capped, line breaks kept."""
    text = "".join(ch for ch in str(value or "") if ch.isprintable() or ch in "\n ")
    return text.strip()[:limit]


def _url(path: str) -> str:
    return get_settings().FRONTEND_URL.rstrip("/") + path


def _minutes(seconds: int) -> int:
    return max(0, seconds) // 60


def _render_lead(db: Session, n: OwnerNotification) -> tuple[str, str] | None:
    try:
        lead_id = uuid.UUID(str(n.payload.get("lead_id")))
    except (ValueError, TypeError):
        return None
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.organization_id == n.organization_id).first()
    if lead is None:
        return None
    who = _one_line(lead.name, 80) or _one_line(lead.phone, 32) or "a caller"
    received = format_local(lead.created_at or _now(), organization_timezone(db, n.organization_id))
    lines = [
        "Your Atla receptionist just took a call and captured a new lead.",
        "",
        f"Name: {_one_line(lead.name, 120) or 'not given'}",
        f"Callback number: {_one_line(lead.phone, 32) or 'not given'}",
        f"Reason for calling: {_block(lead.reason, 500) or 'not given'}",
    ]
    if lead.summary:
        lines += ["", "Call summary:", _block(lead.summary, 1500)]
    lines += ["", f"Received: {received}"]
    if lead.received_after_hours:
        lines.append("After hours: this call came in while your business was closed.")
    lines += [
        f"Open the lead: {_url('/dashboard/leads')}",
        "",
        "Calling back quickly is the best way to win the job.",
    ]
    return f"New lead: {who}", "\n".join(lines)


def _render_service_paused(db: Session, n: OwnerNotification) -> tuple[str, str]:
    s = get_settings()
    reason = n.payload.get("reason")
    if reason == REASON_CREDITS:
        why = (
            "Your call minutes have run out, so your receptionist has been paused. Your phone number is kept.\n\n"
            f"To resume: add minutes at {_url('/dashboard/settings/billing')}, then press Go live at "
            f"{_url('/dashboard/go-live')}."
        )
    elif reason == REASON_PAST_DUE:
        why = (
            "Your last payment could not be collected and the grace period has ended, so your receptionist has "
            "been paused.\n\n"
            f"To resume: update your payment method at {_url('/dashboard/settings/billing')}, then press Go live at "
            f"{_url('/dashboard/go-live')}.\n\n"
            f"We keep your phone number for {s.NUMBER_RELEASE_DELAY_DAYS} days after the pause; after that it is released."
        )
    else:
        why = (
            "Your subscription is no longer active, so your receptionist has been paused.\n\n"
            f"To resume: choose a plan at {_url('/dashboard/settings/billing')}, then press Go live at "
            f"{_url('/dashboard/go-live')}.\n\n"
            f"We keep your phone number for {s.NUMBER_RELEASE_DELAY_DAYS} days after the pause; after that it is released."
        )
    body = (
        "Your Atla receptionist is no longer answering calls.\n\n" + why + "\n\n"
        "Calls to your Atla number are not being answered by the receptionist while it is paused."
    )
    return "Your Atla receptionist has been paused", body


def _render_credits(db: Session, n: OwnerNotification) -> tuple[str, str] | None:
    s = get_settings()
    summary = credit_summary(db, n.organization_id)
    if not has_live_service(db, n.organization_id):
        return None  # already paused: the service_paused email covers it
    if n.kind == KIND_CREDITS_EMPTY:
        if summary.status != "empty":
            return None  # minutes were added since
        return (
            "You have used all your Atla call minutes",
            "You have used all your call minutes for this period.\n\n"
            "Your receptionist is still answering calls for now, but it will pause automatically once you are "
            f"{s.OVERAGE_GRACE_MINUTES} minutes over, and callers will no longer reach it.\n\n"
            f"Add minutes now: {_url('/dashboard/settings/billing')}",
        )
    if summary.status != "low":
        return None
    left = _minutes(summary.available_seconds)
    return (
        "Your Atla call minutes are running low",
        f"You have about {left} minute{'s' if left != 1 else ''} of calls left this period.\n\n"
        "Your receptionist is still answering calls. When the minutes run out it keeps answering for a short grace "
        "and then pauses automatically.\n\n"
        f"Add minutes now: {_url('/dashboard/settings/billing')}",
    )


def _render_payment_failed(db: Session, n: OwnerNotification) -> tuple[str, str] | None:
    sub = (
        db.query(Subscription)
        .filter(
            Subscription.organization_id == n.organization_id,
            Subscription.billing_provider == "paddle",
            Subscription.external_subscription_id == str(n.payload.get("subscription_id")),
        )
        .first()
    )
    if sub is None or sub.status != "past_due":
        return None  # paid since, or cancelled: nothing left to warn about
    since = sub.status_changed_at or sub.last_event_at or n.created_at
    ends = format_local(
        since + timedelta(days=get_settings().PAST_DUE_GRACE_DAYS), organization_timezone(db, n.organization_id)
    )
    return (
        "Action needed: your Atla payment failed",
        "We could not collect your latest Atla payment.\n\n"
        f"Your receptionist keeps answering calls until {ends}. After that it is "
        "paused and callers will no longer reach it.\n\n"
        f"Update your payment method: {_url('/dashboard/settings/billing')}",
    )


def render(db: Session, n: OwnerNotification) -> tuple[str, str] | None:
    """(subject, body), or None when the notification is no longer true and must not be sent."""
    if n.kind == KIND_LEAD:
        return _render_lead(db, n)
    if n.kind == KIND_SERVICE_PAUSED:
        return _render_service_paused(db, n)
    if n.kind in (KIND_LOW_CREDITS, KIND_CREDITS_EMPTY):
        return _render_credits(db, n)
    if n.kind == KIND_PAYMENT_FAILED:
        return _render_payment_failed(db, n)
    raise ValueError(f"unknown notification kind {n.kind!r}")


def recipients(db: Session, organization_id: uuid.UUID) -> list[str]:
    rows = (
        db.query(User.email)
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .filter(OrganizationMember.organization_id == organization_id, OrganizationMember.role.in_((OrgRole.owner, OrgRole.admin)))
        .order_by(User.email)
        .all()
    )
    return [r[0] for r in rows]


# ---------------------------------------------------------------- sending

_RECIPIENT_CONTEXT = b"atla/owner-notification-recipient/v1"


def _recipient_key(secret: str, address: str) -> str:
    """Keyed, non-reversible identifier for one recipient. A plain hash of an email address can be confirmed by anyone who guesses the
    address; this cannot be recomputed without the server's secret. The key is derived from APP_SECRET_KEY for this purpose only
    (domain separation), so the session-signing key itself is never used directly."""
    subkey = hmac.new(secret.encode(), _RECIPIENT_CONTEXT, hashlib.sha256).digest()
    return "h2:" + hmac.new(subkey, address.strip().lower().encode(), hashlib.sha256).hexdigest()[:24]


def _addr_key(address: str) -> str:
    """The identifier recorded for a recipient from now on (always the CURRENT secret)."""
    return _recipient_key(get_settings().APP_SECRET_KEY, address)


def _addr_keys(address: str) -> frozenset[str]:
    """Every identifier under which this recipient may already have been recorded: the current secret, any secret listed in
    APP_SECRET_KEY_PREVIOUS (rotation: so a recipient already emailed is not emailed again), and the unkeyed format of earlier
    drafts of this feature (read-only: it is recognised, never written)."""
    settings = get_settings()
    secrets = [settings.APP_SECRET_KEY, *[k.strip() for k in (settings.APP_SECRET_KEY_PREVIOUS or "").split(",") if k.strip()]]
    legacy = hashlib.sha256(address.strip().lower().encode()).hexdigest()[:16]
    return frozenset([_recipient_key(k, address) for k in secrets] + [legacy])


def _has(recorded: list[str], address: str) -> bool:
    return not _addr_keys(address).isdisjoint(recorded)


def _delivery(row: OwnerNotification) -> dict:
    d = (row.payload or {}).get("_delivery") or {}
    return {"sent": list(d.get("sent", [])), "refused": list(d.get("refused", []))}


def _record(row: OwnerNotification, delivery: dict) -> None:
    payload = dict(row.payload or {})
    payload["_delivery"] = delivery  # a NEW dict, so the JSONB change is always detected
    row.payload = payload


class _LostClaim(Exception):
    """This worker no longer owns the notification (its lease ran out and another worker claimed it). It must stop and write nothing."""


def _claim(db: Session, notification_id: uuid.UUID, now: datetime) -> str | None:
    """Atomically takes ownership of one notification and returns this claim's fencing token (None if someone else owns it).
    ONE conditional UPDATE: it matches only while the row is still pending and due, moves next_attempt_at to the end of a lease, counts
    the attempt and stamps a fresh random token into payload["_claim"], all in the same statement. PostgreSQL takes the row lock for the
    UPDATE; a second worker's identical UPDATE waits, RE-EVALUATES the WHERE against the committed row (READ COMMITTED), no longer
    matches and affects 0 rows. Nothing is read first, so there is no window between "check" and "claim". The caller commits.
    Every later write by this worker is fenced on the token (see _own): a worker whose lease expired and whose row was re-claimed holds
    a token that no longer matches, so it cannot overwrite the new owner's delivery record or final status."""
    token = uuid.uuid4().hex
    result = db.execute(
        update(OwnerNotification)
        .where(
            OwnerNotification.id == notification_id,
            OwnerNotification.status == "pending",
            or_(OwnerNotification.next_attempt_at.is_(None), OwnerNotification.next_attempt_at <= now),
        )
        .values(
            next_attempt_at=now + _LEASE,
            attempts=OwnerNotification.attempts + 1,
            payload=func.jsonb_set(OwnerNotification.payload, literal_column("'{_claim}'::text[]"), func.to_jsonb(cast(token, Text))),
        )
        .execution_options(synchronize_session=False)
    )
    return token if result.rowcount == 1 else None


def _own(db: Session, notification_id: uuid.UUID, token: str) -> OwnerNotification:
    """The row, locked (FOR UPDATE) and freshly read, only if this claim still owns it; otherwise _LostClaim. Every write to a claimed
    row goes through here, so the decision to write is made on the committed state under the row lock, never on an earlier snapshot."""
    row = (
        db.query(OwnerNotification)
        .filter(OwnerNotification.id == notification_id, OwnerNotification.status == "pending", OwnerNotification.payload["_claim"].astext == token)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if row is None:
        db.rollback()
        raise _LostClaim()
    return row


def _process(db: Session, notification_id: uuid.UUID, now: datetime) -> str | None:
    """Sends one notification to each recipient, remembering per recipient (durably, committed right after each accepted send)
    who already has it. A retry therefore only ever goes to recipients still outstanding: nobody gets a second copy because a
    colleague's send failed, and nobody is left out because a colleague's send succeeded.

    Ownership is taken first with an atomic claim (see _claim) and committed BEFORE any email is sent, so no row lock is held
    across SMTP I/O and only the worker whose claim matched ever sends. If that worker dies, the lease runs out (_LEASE) and
    the row is claimed again; recipients already recorded are not emailed again. All writes are fenced (see _own), so a slow worker
    whose lease expired cannot overwrite the new owner's state.

    Guarantees (and limits): the database side is exactly-once per claim. SMTP is at-least-once: a crash or lease expiry between the
    server accepting a message and the ledger commit can send that one recipient a second copy on retry."""
    token = _claim(db, notification_id, now)
    if token is None:
        db.rollback()
        return None  # not pending, not due, or another worker claimed it first
    db.commit()
    try:
        return _run_claimed(db, notification_id, now, token)
    except _LostClaim:
        logger.warning("Owner notification %s was re-claimed by another worker; this worker stops without writing", notification_id)
        return None


def _finish(db: Session, notification_id: uuid.UUID, token: str, **values) -> OwnerNotification:
    row = _own(db, notification_id, token)
    for key, value in values.items():
        setattr(row, key, value)
    db.commit()
    return row


def _run_claimed(db: Session, notification_id: uuid.UUID, now: datetime, token: str) -> str | None:
    row = _own(db, notification_id, token)
    db.commit()
    settings = get_settings()
    try:
        if row.kind not in EMAIL_KINDS:
            # Queued before the policy (or by a stale deploy): it is in-app only. Make sure its in-app notification exists
            # (idempotent, keeps the original time), then skip the email. Never sent.
            notifications.create(db, row.organization_id, row.kind, row.dedupe_key, {k: v for k, v in (row.payload or {}).items() if not k.startswith("_")}, created_at=row.created_at)
            return _finish(db, notification_id, token, status="skipped", last_error="in-app only: operational alerts are not emailed", next_attempt_at=None).status
        built = render(db, row)
        if built is None:
            return _finish(db, notification_id, token, status="skipped", last_error="no longer relevant", next_attempt_at=None).status
        to = recipients(db, row.organization_id)
        if not to:
            return _finish(db, notification_id, token, status="skipped", last_error="no recipients", next_attempt_at=None).status
        subject, body = built
        delivery = _delivery(row)
        results: list[EmailResult] = []
        for address in to:
            if _has(delivery["sent"], address) or _has(delivery["refused"], address):
                continue  # already has it (or can never receive it)
            _own(db, notification_id, token)  # still the owner? (narrows, cannot close, the window while a send is in flight)
            db.rollback()
            result = send_email_result(address, subject, body)
            results.append(result)
            if result.ok or result.outcome is EmailOutcome.PERMANENT:
                fresh = _own(db, notification_id, token)
                delivery = _delivery(fresh)  # merged with the committed ledger, never written from a stale copy
                bucket = delivery["sent" if result.ok else "refused"]
                if not _has(bucket, address):
                    bucket.append(_addr_key(address))
                _record(fresh, delivery)
                db.commit()  # durable now: a crash or a later failure cannot make this recipient get a second copy
            elif result.outcome in (EmailOutcome.QUOTA, EmailOutcome.HELD):
                break  # sending is paused: the remaining recipients are not tried
        outstanding = [a for a in to if not _has(delivery["sent"], a) and not _has(delivery["refused"], a)]
        outcomes = {r.outcome for r in results}
        detail = next((r.detail for r in results if r.detail), "")
        if not outstanding:
            if delivery["sent"]:
                note = "sent; some recipients were permanently refused by the mail server" if delivery["refused"] else None
                return _finish(db, notification_id, token, status="sent", sent_at=now, last_error=note, next_attempt_at=None).status
            if outcomes <= {EmailOutcome.BLOCKED} and outcomes:
                return _finish(db, notification_id, token, status="skipped", last_error="test mode: email is never sent", next_attempt_at=None).status
            # Everyone was permanently refused: another attempt gets the same answer. Fail now, visibly.
            return _finish(db, notification_id, token, status="failed", last_error=f"permanent email failure ({detail})"[:500], next_attempt_at=None).status
        if outcomes == {EmailOutcome.BLOCKED}:
            return _finish(db, notification_id, token, status="skipped", last_error="test mode: email is never sent", next_attempt_at=None).status
        quota = next((r for r in results if r.outcome is EmailOutcome.QUOTA), None)
        if quota is not None:
            # Waiting for a quota is not a failed attempt and must not use up the retries. The notification stays pending, with
            # its delivered recipients remembered, and is due again when sending may resume.
            fresh = _own(db, notification_id, token)
            fresh.attempts = max(0, fresh.attempts - 1)
            if quota.retry_after is not None:
                fresh.next_attempt_at = quota.retry_after
                fresh.last_error = f"email {quota.quota_kind} quota exhausted; paused until {quota.retry_after.isoformat()}"[:500]
            else:
                fresh.next_attempt_at = now + _OPERATOR_RECHECK
                fresh.last_error = f"email {quota.quota_kind} quota exhausted; waiting for an operator to resume sending"[:500]
            db.commit()
            return fresh.status
        if EmailOutcome.HELD in outcomes:
            fresh = _own(db, notification_id, token)
            fresh.attempts = max(0, fresh.attempts - 1)
            fresh.next_attempt_at, fresh.last_error = now + _HELD_RECHECK, "email held: the quota breaker (database) cannot be read"
            db.commit()
            return fresh.status
        error = "email was not accepted by the mail server" + (f" ({detail})" if detail else "")
        error += f"; {len(outstanding)} of {len(to)} recipients still to receive it" if (delivery["sent"] or delivery["refused"]) else ""
    except _LostClaim:
        raise
    except Exception as exc:  # noqa: BLE001 - recorded and retried, never raised into the worker pass
        db.rollback()
        error = f"{type(exc).__name__}: {exc}"[:500]
        logger.exception("Owner notification %s could not be built or sent", notification_id)
    fresh = _own(db, notification_id, token)
    fresh.last_error = error[:500]
    if fresh.attempts >= settings.OWNER_NOTIFICATION_MAX_ATTEMPTS:
        fresh.status, fresh.next_attempt_at = "failed", None
    else:
        delay = min(_BACKOFF_BASE_SECONDS * 2 ** (fresh.attempts - 1), _BACKOFF_CAP_SECONDS)
        fresh.next_attempt_at = now + timedelta(seconds=delay)
    db.commit()
    return fresh.status


def send_due_notifications(db: Session, limit: int = 50, now: datetime | None = None) -> dict:
    """Worker entry. Sends pending notifications that are due. One short transaction per notification."""
    now = now or _now()
    result = {"sent": 0, "skipped": 0, "failed": 0, "retry": 0}
    if not get_settings().OWNER_NOTIFICATIONS_ENABLED:
        return result
    state = check_breaker(now)
    if not state.available:
        # The database cannot say whether a quota failure was recorded. Fail closed: nothing is attempted and nothing is touched.
        result["breaker_unavailable"] = True
        return result
    if state.pause is not None:
        # The provider's quota is used up. Nothing is attempted and nothing is touched: the rows stay pending and go out
        # when the pause ends (or an operator clears it: python -m app.workers.email_quota clear).
        result["paused_until"] = state.pause.until.isoformat() if state.pause.until else "operator"
        result["quota_kind"] = state.pause.kind
        return result
    ids = [
        r[0]
        for r in db.query(OwnerNotification.id)
        .filter(
            OwnerNotification.status == "pending",
            (OwnerNotification.next_attempt_at.is_(None)) | (OwnerNotification.next_attempt_at <= now),
        )
        .order_by(OwnerNotification.created_at)
        .limit(limit)
        .all()
    ]
    in_a_row = 0
    for notification_id in ids:
        outcome = _process(db, notification_id, now)
        if outcome == "sent":
            result["sent"] += 1
            in_a_row = 0
        elif outcome == "skipped":
            result["skipped"] += 1
        elif outcome == "failed":
            result["failed"] += 1
        elif outcome == "pending":
            result["retry"] += 1
            in_a_row += 1
            stop = check_breaker(now)
            if in_a_row >= _MAX_CONSECUTIVE_FAILURES or stop.pause is not None or not stop.available:
                result["stopped_early"] = True  # leave the rest for the next pass instead of hammering a refusing provider
                break
    return result
