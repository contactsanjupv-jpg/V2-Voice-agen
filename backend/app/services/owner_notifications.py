"""
Tell the business owner what the business needs to know, by email.

  lead             a call captured a new lead (real calls only, never a test call)
  service_paused   the receptionist was switched off: credits spent, payment failed or subscription ended
  low_credits      the plan's minutes are running low (calls are still being answered)
  credits_empty    the minutes are used up; the receptionist pauses itself once the overage grace is also spent
  payment_failed   Paddle reports the subscription past due

How it stays reliable (an outbox, not a fire-and-forget send):
  * enqueue() writes one row in the SAME transaction as the thing that caused it. A rolled-back cause leaves no
    notification; a committed cause always has one. UNIQUE(organization_id, kind, dedupe_key) makes a repeated cause
    (webhook redelivery, sweeper retry, reconciler) a no-op.
  * send_due_notifications() is run by the worker. It builds the message from LIVE data at send time (the row holds
    ids only, no customer text), skips a notification that is no longer true (the payment was fixed, credits were
    bought), and retries a failed send with backoff. Webhooks and the call pipeline never wait for SMTP.
  * Delivery is at-least-once: a crash between the SMTP hand-off and the commit can send one email twice.

Recipients are the organization's owners and admins, resolved when the email is sent.
The message never contains a transcript. A lead email does contain what the caller said about themselves (name,
callback number, reason) because that is the point of it.
"""
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.email import send_email
from app.db.models.billing import Subscription
from app.db.models.crm import Lead
from app.db.models.notifications import OwnerNotification
from app.db.models.tenancy import OrganizationMember, OrgRole, User
from app.db.models.telephony import PhoneNumber, PhoneNumberStatus
from app.db.models.voice_agent import Agent, AgentStatus
from app.services.billing_state import has_active_subscription
from app.services.business_time import format_local, organization_timezone
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

_BACKOFF_BASE_SECONDS = 60
_BACKOFF_CAP_SECONDS = 3600


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- enqueue

def enqueue(db: Session, organization_id: uuid.UUID, kind: str, dedupe_key: str, payload: dict | None = None) -> bool:
    """Record that the owner must be told. True if this call created the row.

    Never raises and never poisons the caller's transaction (it runs in a savepoint): a notification problem must not
    fail a webhook, a call ingest or an entitlement pass. The row commits or rolls back with the caller."""
    if not get_settings().OWNER_NOTIFICATIONS_ENABLED:
        return False
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
        return True
    except IntegrityError:
        return False  # already recorded: this is the same cause seen again
    except Exception:  # noqa: BLE001
        logger.exception("Could not record an owner notification (kind=%s)", kind)
        return False


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

def _process(db: Session, notification_id: uuid.UUID, now: datetime) -> str | None:
    row = (
        db.query(OwnerNotification)
        .filter(OwnerNotification.id == notification_id)
        .with_for_update(skip_locked=True)  # another worker has it
        .first()
    )
    if row is None or row.status != "pending":
        return None
    settings = get_settings()
    row.attempts += 1
    try:
        built = render(db, row)
        if built is None:
            row.status, row.last_error, row.next_attempt_at = "skipped", "no longer relevant", None
            db.commit()
            return "skipped"
        to = recipients(db, row.organization_id)
        if not to:
            row.status, row.last_error, row.next_attempt_at = "skipped", "no recipients", None
            db.commit()
            return "skipped"
        subject, body = built
        delivered = [send_email(address, subject, body) for address in to]  # every recipient, not just the first
        if any(delivered):
            row.status, row.sent_at, row.last_error, row.next_attempt_at = "sent", now, None, None
            db.commit()
            return "sent"
        error = "email was not accepted by the mail server"
    except Exception as exc:  # noqa: BLE001 - recorded and retried, never raised into the worker pass
        db.rollback()
        row = db.query(OwnerNotification).filter(OwnerNotification.id == notification_id).first()
        if row is None or row.status != "pending":
            return None
        row.attempts = (row.attempts or 0) + 1
        error = f"{type(exc).__name__}: {exc}"[:500]
        logger.exception("Owner notification %s could not be built or sent", notification_id)
    row.last_error = error[:500]
    if row.attempts >= settings.OWNER_NOTIFICATION_MAX_ATTEMPTS:
        row.status, row.next_attempt_at = "failed", None
    else:
        delay = min(_BACKOFF_BASE_SECONDS * 2 ** (row.attempts - 1), _BACKOFF_CAP_SECONDS)
        row.next_attempt_at = now + timedelta(seconds=delay)
    db.commit()
    return row.status


def send_due_notifications(db: Session, limit: int = 50, now: datetime | None = None) -> dict:
    """Worker entry. Sends pending notifications that are due. One short transaction per notification."""
    now = now or _now()
    result = {"sent": 0, "skipped": 0, "failed": 0, "retry": 0}
    if not get_settings().OWNER_NOTIFICATIONS_ENABLED:
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
    for notification_id in ids:
        outcome = _process(db, notification_id, now)
        if outcome == "sent":
            result["sent"] += 1
        elif outcome == "skipped":
            result["skipped"] += 1
        elif outcome == "failed":
            result["failed"] += 1
        elif outcome == "pending":
            result["retry"] += 1
    return result
