"""
In-app notifications: what the business needs to know, shown in the dashboard Overview side panel. Never emailed.

Policy (see docs/NOTIFICATIONS.md): email is reserved for password recovery and security alerts, which the account endpoints send.
Every other customer notification (leads, credits, billing, service state, and later reports and product news) is in-app only.

Reliability:
  * create() inserts ONE row in the SAME transaction as the event that caused it (a savepoint, so a notification problem
    can never fail or poison the caller). A rolled-back cause leaves no notification; a committed cause always has one.
  * INSERT ... ON CONFLICT DO NOTHING on UNIQUE(organization_id, kind, dedupe_key): a repeated cause (webhook redelivery,
    sweeper retry, two workers racing) is a database-level no-op, never an error and never a duplicate.
  * An email failure cannot affect any of this: the email outbox (owner_notifications) is a separate table written in its own
    savepoint after the in-app row.

Tenancy and authorization:
  * one row per RECIPIENT; every read and write is scoped to (the caller's user id, the organization of their verified
    membership), so the organization is never taken from client input and no one can name another person's row;
  * audience is fixed when the notification is created (owners and admins for billing and service state; every member for
    leads), so a member never receives billing notifications;
  * a notification that is not the caller's own is "not found", the same answer as one that does not exist.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, exists, func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models.crm import Lead
from app.db.models.notifications import Notification, NotificationPreference
from app.db.models.tenancy import OrganizationMember, OrgRole

logger = logging.getLogger("atla.notifications")

MAX_PAGE = 50
RETENTION = timedelta(days=180)
LINK_PATTERN = re.compile(r"^/dashboard(/[A-Za-z0-9_-]+)*([?][A-Za-z0-9_=&.-]*)?(#[A-Za-z0-9_-]*)?$")  # relative, in-app only: no scheme, no host, no "//", no spaces

BILLING_LINK = "/dashboard/settings/billing"
LEADS_LINK = "/dashboard/leads"
OVERVIEW_LINK = "/dashboard"
CALLS_LINK = "/dashboard/calls"
GO_LIVE_LINK = "/dashboard/go-live"

# Who is told. Resolved when the notification is created: the owners and admins of the organization, or every member.
AUDIENCE_ADMINS = (OrgRole.owner, OrgRole.admin)
AUDIENCE_ALL = (OrgRole.owner, OrgRole.admin, OrgRole.member)

_PAUSE_BODIES = {
    "credits_exhausted": "Your plan's call minutes are used up, so your receptionist was paused.",
    "payment_past_due": "Your latest payment failed, so your receptionist was paused.",
    "subscription_lapsed": "Your subscription ended, so your receptionist was paused.",
}


def safe_link(link: str | None) -> str | None:
    """A deep link is only ever a relative dashboard path. Anything else is refused (None)."""
    if link is None:
        return None
    return link if LINK_PATTERN.fullmatch(link) and len(link) <= 255 else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _one_line(value, limit: int) -> str:
    text = "".join(ch for ch in str(value or "") if ch.isprintable() or ch.isspace())
    return " ".join(text.split())[:limit]


# kind -> (category, severity, link, audience). The only notifications that can exist.
CATALOGUE: dict[str, tuple[str, str, str, tuple]] = {
    "lead": ("leads", "info", LEADS_LINK, AUDIENCE_ALL),
    "service_paused": ("receptionist", "critical", BILLING_LINK, AUDIENCE_ADMINS),
    "low_credits": ("credits", "warning", BILLING_LINK, AUDIENCE_ADMINS),
    "credits_empty": ("credits", "critical", BILLING_LINK, AUDIENCE_ADMINS),
    "payment_failed": ("billing", "critical", BILLING_LINK, AUDIENCE_ADMINS),
    # Billing and credit changes (Task 5). In-app only, like every operational notification.
    "subscription_started": ("billing", "info", BILLING_LINK, AUDIENCE_ADMINS),
    "plan_changed": ("billing", "info", BILLING_LINK, AUDIENCE_ADMINS),
    "cancellation_scheduled": ("billing", "warning", BILLING_LINK, AUDIENCE_ADMINS),
    "subscription_canceled": ("billing", "warning", BILLING_LINK, AUDIENCE_ADMINS),
    "payment_recovered": ("billing", "info", BILLING_LINK, AUDIENCE_ADMINS),
    "refund_applied": ("billing", "info", BILLING_LINK, AUDIENCE_ADMINS),
    "chargeback_applied": ("billing", "warning", BILLING_LINK, AUDIENCE_ADMINS),
    "plan_minutes_added": ("credits", "info", BILLING_LINK, AUDIENCE_ADMINS),
    "topup_credited": ("credits", "info", BILLING_LINK, AUDIENCE_ADMINS),
    # Call, receptionist and phone activity (Task 6). In-app only.
    "missed_call": ("calls", "warning", CALLS_LINK, AUDIENCE_ALL),
    "call_transferred": ("calls", "info", CALLS_LINK, AUDIENCE_ALL),
    "receptionist_live": ("receptionist", "info", GO_LIVE_LINK, AUDIENCE_ADMINS),
    "phone_number_ready": ("phone", "info", GO_LIVE_LINK, AUDIENCE_ADMINS),
    "phone_provisioning_failed": ("phone", "critical", GO_LIVE_LINK, AUDIENCE_ADMINS),
    "phone_number_released": ("phone", "warning", GO_LIVE_LINK, AUDIENCE_ADMINS),
    # Daily analytics (Task 7). In-app only: there is no analytics email.
    "daily_report": ("analytics", "info", OVERVIEW_LINK, AUDIENCE_ADMINS),
}


def _count(value) -> int | None:
    """A non-negative whole number from a payload, or None. Only integers ever reach a message."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if 0 <= n < 100_000_000 else None


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _compose_report(kind: str, payload: dict) -> tuple[str, str] | None:
    """The daily report: the date is parsed and re-formatted by us, every figure is an integer, the wording is ours."""
    if kind != "daily_report":
        return None
    try:
        day = datetime.strptime(str(payload.get("date")), "%Y-%m-%d").date()
        label = f"{day.strftime('%a')}, {day.strftime('%b')} {day.day}"
    except ValueError:
        label = None
    calls, leads = _count(payload.get("calls")), _count(payload.get("leads"))
    minutes, before = _count(payload.get("minutes")), _count(payload.get("previous_calls"))
    title = f"Daily report: {label}" if label else "Daily report"
    if calls is None or leads is None:
        return title, "Your daily activity report is ready."
    parts = [_plural(calls, "call") + (f" ({minutes} min)" if minutes else ""), _plural(leads, "new lead")]
    body = f"{label or 'The day'}: " + ", ".join(parts) + "."
    if before is not None:
        body += f" The day before: {_plural(before, 'call')}."
    return title, body


def _last4(value) -> str | None:
    """The last four digits of a caller's number, or None. Only digits ever reach a message."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-4:] if len(digits) >= 4 else None


def _compose_activity(kind: str, payload: dict) -> tuple[str, str] | None:
    reason = str(payload.get("reason") or "")
    if kind == "missed_call":
        tail = _last4(payload.get("caller_last4"))
        who = f"A caller (number ending {tail})" if tail else "A caller"
        return "Missed call", f"{who} could not be connected to your receptionist."
    if kind == "call_transferred":
        tail = _last4(payload.get("caller_last4"))
        who = f"A call from a number ending {tail}" if tail else "A call"
        return "Call transferred", f"{who} was transferred to your team."
    if kind == "receptionist_live":
        return "Receptionist is live", "Your receptionist is now answering calls on your business number."
    if kind == "phone_number_ready":
        return "Phone number ready", "Your business phone number is set up."
    if kind == "phone_provisioning_failed":
        if reason == "blocked_payment":
            return "Phone number couldn't be set up", "Setting up your number hit a payment problem on our side. Please contact support."
        return "Phone number couldn't be set up", "We couldn't set up your business number. Try again from the Go live page, or contact support."
    if kind == "phone_number_released":
        why = "because your subscription ended" if reason == "entitlement" else "as requested"
        return "Phone number released", f"Your business number was released {why}. Your receptionist is paused until you get a new number."
    return None


def _plan_name(plan_id) -> str | None:
    """The customer-facing plan name from OUR plan table (never from a payload's free text); None for an unknown plan."""
    from app.services.plans import PLANS

    plan = PLANS.get(str(plan_id)) if plan_id else None
    return plan.name if plan is not None else None


def _minutes(value) -> int | None:
    """Whole minutes from a payload number, or None. Only integers ever reach a message."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if 0 < n < 10_000_000 else None


def _compose_billing(kind: str, payload: dict) -> tuple[str, str] | None:
    plan = _plan_name(payload.get("plan_id"))
    previous = _plan_name(payload.get("previous_plan_id"))
    minutes = _minutes(payload.get("minutes"))
    if kind == "subscription_started":
        return "Subscription started", f"Your {plan} plan is active." if plan else "Your subscription is active."
    if kind == "plan_changed":
        if plan and previous:
            return "Plan changed", f"Your plan changed from {previous} to {plan}."
        return "Plan changed", f"Your plan is now {plan}." if plan else "Your plan was changed."
    if kind == "cancellation_scheduled":
        return "Cancellation scheduled", "Your subscription will end at the close of the current billing period. Your receptionist keeps working until then."
    if kind == "subscription_canceled":
        return "Subscription canceled", "Your subscription has ended. Your receptionist pauses until you subscribe again."
    if kind == "payment_recovered":
        return "Payment received", "Your payment went through and your subscription is active again."
    if kind == "refund_applied":
        return "Refund applied", f"A refund was applied and {minutes} purchased minutes were removed." if minutes else "A refund was applied to a purchase."
    if kind == "chargeback_applied":
        return "Payment disputed", f"A chargeback was applied and {minutes} purchased minutes were removed." if minutes else "A payment dispute (chargeback) was applied to a purchase."
    if kind == "plan_minutes_added":
        return "Plan minutes added", f"{minutes} minutes were added for this billing period." if minutes else "Your plan's minutes were added for this billing period."
    if kind == "topup_credited":
        return "Top-up added", f"{minutes} minutes were added to your balance." if minutes else "Your top-up was added to your balance."
    return None


def _compose(db: Session, organization_id: uuid.UUID, kind: str, payload: dict) -> tuple[str, str]:
    """Title and body, built server-side from a fixed catalogue. Caller-supplied text (a lead's name) is reduced to one
    safe line; nothing else from a customer or a transcript is ever shown."""
    if kind == "lead":
        name = ""
        try:
            lead = db.query(Lead).filter(Lead.id == uuid.UUID(str(payload.get("lead_id"))), Lead.organization_id == organization_id).first()
            name = _one_line(lead.name, 80) if lead is not None else ""
        except (ValueError, TypeError):
            pass
        return "New lead captured", f"{name} left their details." if name else "A caller left their details."
    if kind == "service_paused":
        return "Receptionist paused", _PAUSE_BODIES.get(str(payload.get("reason")), "Your receptionist was paused.")
    if kind == "low_credits":
        return "Call minutes running low", "Your plan's minutes are almost used up. Calls are still being answered."
    if kind == "credits_empty":
        return "Call minutes used up", "Your plan's minutes are used up. Your receptionist pauses once any overage allowance is also spent."
    if kind == "payment_failed":
        return "Payment failed", "Your latest payment didn't go through. Update your payment method to keep your receptionist running."
    for compose in (_compose_billing, _compose_activity, _compose_report):
        composed = compose(kind, payload)
        if composed is not None:
            return composed
    raise KeyError(kind)


# ---------------------------------------------------------------- create

def create(
    db: Session,
    organization_id: uuid.UUID,
    kind: str,
    dedupe_key: str,
    payload: dict | None = None,
    created_at: datetime | None = None,
) -> bool:
    """Record one in-app notification for every recipient in the audience. True if THIS call created any.

    One INSERT ... SELECT over the organization's members with ON CONFLICT DO NOTHING: atomic, so a repeated or concurrent call
    for the same cause creates nothing the second time and never a duplicate or an error.
    Never raises and never poisons the caller's transaction (savepoint): a notification problem must not fail a webhook, a call
    ingest or an entitlement pass. Commits or rolls back with the caller. Unknown kinds are refused (logged)."""
    spec = CATALOGUE.get(kind)
    if spec is None:
        logger.error("Unknown notification kind %r: not recorded", kind)
        return False
    category, severity, link, audience = spec
    payload = payload or {}
    when = created_at or _now()
    try:
        with db.begin_nested():
            title, body = _compose(db, organization_id, kind, payload)
            recipients = select(
                literal(organization_id), OrganizationMember.user_id, literal(kind), literal(category), literal(severity), literal(title), literal(body),
                literal(safe_link(link)), literal(str(dedupe_key)[:255]), literal(payload, type_=JSONB), literal(when), literal(when), func.gen_random_uuid(),
            ).where(OrganizationMember.organization_id == organization_id, OrganizationMember.role.in_(audience))
            if severity != "critical":  # critical notifications are always delivered; the rest honour each person's mute
                recipients = recipients.where(~exists().where(
                    NotificationPreference.organization_id == organization_id,
                    NotificationPreference.user_id == OrganizationMember.user_id,
                    NotificationPreference.category == category,
                    NotificationPreference.enabled.is_(False),
                ))
            result = db.execute(
                pg_insert(Notification)
                .from_select(
                    ["organization_id", "recipient_user_id", "kind", "category", "severity", "title", "body", "link", "dedupe_key", "payload", "created_at", "updated_at", "id"],
                    recipients,
                )
                .on_conflict_do_nothing(constraint="uq_notification_key")
                .returning(Notification.id)
            )
            return result.first() is not None
    except Exception:  # noqa: BLE001
        logger.exception("Could not record an in-app notification (kind=%s)", kind)
        return False


# ---------------------------------------------------------------- preferences
# In-app only, per person, per category, per organization. No row means on. A mute stops NEW notifications of that category from
# being created for that person (history is untouched); critical notifications ignore it. Always scoped to the caller's own
# membership: there is no way to read or change someone else's preferences.

def _critical_categories() -> set[str]:
    return {category for category, severity, _link, _aud in CATALOGUE.values() if severity == "critical"}


def get_preferences(db: Session, membership: OrganizationMember) -> list[dict]:
    muted = set(
        db.execute(
            select(NotificationPreference.category).where(
                NotificationPreference.organization_id == membership.organization_id,
                NotificationPreference.user_id == membership.user_id,
                NotificationPreference.enabled.is_(False),
            )
        ).scalars()
    )
    critical = _critical_categories()
    from app.db.models.notifications import CATEGORIES

    return [{"category": c, "enabled": c not in muted, "critical_always_delivered": c in critical} for c in CATEGORIES]


def set_preference(db: Session, membership: OrganizationMember, category: str, enabled: bool) -> dict:
    from app.db.models.notifications import CATEGORIES

    if category not in CATEGORIES:
        raise ValueError(f"unknown category {category!r}")
    stmt = pg_insert(NotificationPreference).values(
        organization_id=membership.organization_id, user_id=membership.user_id, category=category, enabled=enabled, id=uuid.uuid4(),
    )
    db.execute(stmt.on_conflict_do_update(constraint="uq_notification_preference", set_={"enabled": enabled, "updated_at": _now()}))
    db.commit()
    return next(p for p in get_preferences(db, membership) if p["category"] == category)


# ---------------------------------------------------------------- reading
# Every query is scoped to (the caller's user, the organization of their membership). The organization comes from the verified
# membership, never from the request body, and the recipient is the caller: there is no way to name someone else's row.

def _mine(user_id: uuid.UUID, organization_id: uuid.UUID):
    return and_(Notification.recipient_user_id == user_id, Notification.organization_id == organization_id)


def unread_count(db: Session, membership: OrganizationMember, category: str | None = None) -> int:
    stmt = select(func.count()).select_from(Notification).where(_mine(membership.user_id, membership.organization_id), Notification.read_at.is_(None))
    if category:
        stmt = stmt.where(Notification.category == category)
    return int(db.execute(stmt).scalar() or 0)


def latest_seq(db: Session, membership: OrganizationMember) -> int:
    return int(db.execute(select(func.coalesce(func.max(Notification.seq), 0)).where(_mine(membership.user_id, membership.organization_id))).scalar() or 0)


def state(db: Session, membership: OrganizationMember) -> dict:
    """What changes when something happens (real-time updates): the newest notification and the caller's unread count."""
    return {"latest_seq": latest_seq(db, membership), "unread_count": unread_count(db, membership)}


def serialize(n: Notification) -> dict:
    return {
        "id": str(n.id),
        "seq": n.seq,
        "kind": n.kind,
        "category": n.category,
        "severity": n.severity,
        "title": n.title,
        "body": n.body,
        "link": safe_link(n.link),
        "created_at": n.created_at,
        "read_at": n.read_at,
        "is_read": n.read_at is not None,
    }


def list_page(
    db: Session,
    membership: OrganizationMember,
    limit: int = 20,
    before: int | None = None,
    unread_only: bool = False,
    category: str | None = None,
) -> dict:
    limit = max(1, min(limit, MAX_PAGE))
    stmt = select(Notification).where(_mine(membership.user_id, membership.organization_id))
    if before is not None:
        stmt = stmt.where(Notification.seq < before)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    if category:
        stmt = stmt.where(Notification.category == category)
    rows = list(db.execute(stmt.order_by(Notification.seq.desc()).limit(limit + 1)).scalars())
    page = rows[:limit]
    return {
        "items": [serialize(n) for n in page],
        "next_cursor": page[-1].seq if len(rows) > limit else None,
        "unread_count": unread_count(db, membership),
    }


# ---------------------------------------------------------------- read state

def mark_read(db: Session, membership: OrganizationMember, notification_id: uuid.UUID) -> bool:
    """Mark one notification read. False (nothing written) unless it is the caller's own, in the caller's organization: another
    tenant's, another member's and one that never existed all look the same. Idempotent: the first read time is kept."""
    owned = db.execute(select(Notification.id).where(Notification.id == notification_id, _mine(membership.user_id, membership.organization_id))).first()
    if owned is None:
        return False
    db.execute(
        update(Notification)
        .where(Notification.id == notification_id, _mine(membership.user_id, membership.organization_id), Notification.read_at.is_(None))
        .values(read_at=_now())
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return True


def mark_all_read(db: Session, membership: OrganizationMember, category: str | None = None) -> int:
    """Mark everything the caller has not read as read, in ONE statement. Notifications that arrive while it runs are not in its
    snapshot and stay unread. Returns how many were marked."""
    stmt = update(Notification).where(_mine(membership.user_id, membership.organization_id), Notification.read_at.is_(None))
    if category:
        stmt = stmt.where(Notification.category == category)
    result = db.execute(stmt.values(read_at=_now()).execution_options(synchronize_session=False))
    db.commit()
    return result.rowcount or 0


# ---------------------------------------------------------------- housekeeping

_last_prune: float | None = None


def prune(db: Session, now: datetime | None = None) -> int:
    """Delete notifications older than the retention window (their read marks go with them)."""
    cutoff = (now or _now()) - RETENTION
    removed = db.execute(delete(Notification).where(Notification.created_at < cutoff)).rowcount or 0
    db.commit()
    return removed


def prune_pass(db: Session) -> int | None:
    """Worker entry: at most once an hour."""
    global _last_prune
    if _last_prune is not None and time.monotonic() - _last_prune < 3600:
        return None
    _last_prune = time.monotonic()
    return prune(db)
