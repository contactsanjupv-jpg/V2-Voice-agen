"""Account essentials: password reset/change, account deletion."""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.auth.deps import current_membership, get_current_user
from app.auth.passwords import hash_password, verify_password
from app.auth.rate_limit import RateLimitExceeded, check_rate_limit
from app.auth.reset_tokens import consume_reset_token, create_reset_token
from app.auth.sessions import destroy_all_sessions_for_user
from app.config import get_settings
from app.core.email import send_email
from app.db.base import get_db
from app.db.models.tenancy import Organization, OrganizationMember, OrgRole, User
from app.services.account_deletion import AccountDeletionBlocked, delete_organization
from app.api.v1.auth import _set_session_cookie

router = APIRouter(prefix="/api/v1", tags=["account"])
settings = get_settings()
logger = logging.getLogger("atla.account")

_Password = Field(min_length=10, max_length=128)


class ResetRequest(BaseModel):
    email: EmailStr


class ResetConfirm(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    new_password: str = _Password


class ChangePassword(BaseModel):
    current_password: str = Field(max_length=128)
    new_password: str = _Password


class DeleteAccount(BaseModel):
    confirm_name: str
    password: str = Field(max_length=128)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/auth/password-reset/request", status_code=status.HTTP_204_NO_CONTENT)
def request_password_reset(payload: ResetRequest, request: Request, db: Session = Depends(get_db)):
    """Always 204 — never reveals whether the address has an account."""
    email = payload.email.lower()
    try:
        check_rate_limit(f"pwreset-ip:{_client_ip(request)}", 10, 3600)
        check_rate_limit(f"pwreset-email:{email}", 3, 3600)
    except RateLimitExceeded:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    user = db.query(User).filter(User.email == email).first()
    if user is not None:
        token = create_reset_token(str(user.id))
        link = f"{settings.FRONTEND_URL}/reset-password?token={token}"
        send_email(
            user.email,
            "Reset your Atla password",
            f"Use this link to choose a new password (valid for {settings.PASSWORD_RESET_TTL_SECONDS // 60} minutes):\n\n{link}\n\n"
            "If you didn't ask for this, you can ignore this email.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/auth/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
def confirm_password_reset(payload: ResetConfirm, request: Request, db: Session = Depends(get_db)):
    try:
        check_rate_limit(f"pwreset-confirm:{_client_ip(request)}", 20, 3600)
    except RateLimitExceeded as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Please try again later.") from e
    user_id = consume_reset_token(payload.token)
    user = db.get(User, uuid.UUID(user_id)) if user_id else None
    if user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This reset link is invalid or has expired.")
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    destroy_all_sessions_for_user(str(user.id))  # anyone holding an old session is signed out
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/auth/password/change", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    payload: ChangePassword, response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    try:
        check_rate_limit(f"pwchange:{user.id}", 10, 900)
    except RateLimitExceeded as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Please try again later.") from e
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Your current password is incorrect.")
    if payload.new_password == payload.current_password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose a password you haven't used just now.")
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    destroy_all_sessions_for_user(str(user.id))  # every device signs out, including stolen sessions
    _set_session_cookie(response, str(user.id))  # ...except this one, which gets a fresh session
    response.status_code = status.HTTP_204_NO_CONTENT


@router.post("/orgs/{organization_id}/delete", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    payload: DeleteAccount,
    membership: OrganizationMember = Depends(current_membership),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Owner-only, re-authenticated, and the org name must be typed to confirm."""
    if membership.role != OrgRole.owner:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the account owner can delete the account.")
    try:
        check_rate_limit(f"delete-account:{membership.organization_id}", 5, 3600)
    except RateLimitExceeded as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Please try again later.") from e
    org = db.get(Organization, membership.organization_id)
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Your password is incorrect.")
    if org is None or payload.confirm_name.strip() != org.name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Type your business name exactly to confirm.")
    try:
        delete_organization(db, membership.organization_id)
    except AccountDeletionBlocked as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "We couldn't finish closing your account yet, so nothing was deleted. Please try again in a moment.",
        ) from e
