"""
FastAPI dependencies for auth + tenant isolation + RBAC.

The rule this file exists to enforce: organization_id for authorization
purposes ALWAYS comes from the authenticated session's active membership,
NEVER from a path/query/body parameter the browser sent. A route handler
that needs "the current org" calls `current_membership`, not
`request.path_params["organization_id"]`.
"""
import uuid

from fastapi import Cookie, Depends, HTTPException, Path, status
from sqlalchemy.orm import Session

from app.auth.sessions import read_session
from app.config import get_settings
from app.db.base import get_db
from app.db.models.tenancy import OrganizationMember, OrgRole, User, role_at_least

settings = get_settings()


def get_current_user(
    db: Session = Depends(get_db),
    session_cookie: str | None = Cookie(default=None, alias=settings.SESSION_COOKIE_NAME),
) -> User:
    if session_cookie is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    data = read_session(session_cookie)
    if data is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired or invalid")
    user = db.get(User, uuid.UUID(data["user_id"]))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    return user


def current_membership(
    organization_id: uuid.UUID = Path(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OrganizationMember:
    """
    Resolves (and authorizes) the caller's membership in the org named in
    the URL path. This is the ONLY place organization_id from the URL is
    trusted — and it's trusted only as "which org is the caller asking
    about", with membership itself proving they're allowed to ask.
    Every downstream query still filters by membership.organization_id,
    never by the raw path parameter directly.
    """
    membership = (
        db.query(OrganizationMember)
        .filter(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id == user.id,
        )
        .first()
    )
    if membership is None:
        # Same 404 whether the org doesn't exist or the user just isn't a
        # member of it — do not leak which org IDs exist to non-members.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organization not found")
    return membership


def require_role(minimum: OrgRole):
    """
    Usage: Depends(require_role(OrgRole.admin)) on any mutating endpoint.
    A Member calling a billing/admin endpoint directly (bypassing the UI)
    gets a 403 here — the frontend hiding a button is never the actual
    control.
    """

    def _check(membership: OrganizationMember = Depends(current_membership)) -> OrganizationMember:
        if not role_at_least(membership.role, minimum):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")
        return membership

    return _check


def require_platform_admin(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> User:
    from app.db.models.platform import AdminUser

    is_admin = db.query(AdminUser).filter(AdminUser.user_id == user.id).first() is not None
    if not is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return user


def has_active_subscription(db: Session, organization_id) -> bool:
    """Kept here for existing imports; the logic lives in services/billing_state."""
    from app.services.billing_state import has_active_subscription as _has_active

    return _has_active(db, organization_id)


def require_active_subscription(
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
) -> OrganizationMember:
    """
    The money gate. Every action that costs US real money with an
    external provider sits behind this — checked server-side, never
    trusted from the frontend. A trialing or active subscription
    passes; anything else is rejected with 402 before any provider call.
    """
    if not has_active_subscription(db, membership.organization_id):
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            "An active subscription is required for this action. Please choose a plan to continue.",
        )
    return membership


def require_feature(feature):
    """
    Server-side plan gate. Passes only when the org has a trialing/active
    subscription whose plan includes `feature` (services/plans.py). The frontend
    is never the authority; a customer calling the endpoint directly gets 402.
    """
    from app.services.plans import active_plan, lowest_plan_with

    def _dependency(
        membership: OrganizationMember = Depends(current_membership),
        db: Session = Depends(get_db),
    ) -> OrganizationMember:
        if not has_active_subscription(db, membership.organization_id):
            raise HTTPException(
                status.HTTP_402_PAYMENT_REQUIRED,
                "An active subscription is required for this action. Please choose a plan to continue.",
            )
        plan = active_plan(db, membership.organization_id)
        if plan is None or feature not in plan.features:
            needed = lowest_plan_with(feature)
            message = (
                f"Your plan doesn't include this feature. Upgrade to {needed.name}."
                if needed is not None and plan is not None
                else "Your plan doesn't include this feature."
            )
            raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, message)
        return membership

    return _dependency