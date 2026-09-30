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
from app.db.models.tenancy import Organization, OrganizationMember, OrgRole, User, role_at_least

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
    from app.db.models.billing import Subscription

    subscription = (
        db.query(Subscription)
        .filter(Subscription.organization_id == membership.organization_id)
        .order_by(Subscription.created_at.desc())
        .first()
    )
    if subscription is None or subscription.status not in ("trialing", "active"):
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            "An active subscription is required for this action. Please choose a plan to continue.",
        )
    return membership