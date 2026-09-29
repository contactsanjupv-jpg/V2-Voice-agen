from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.auth.passwords import hash_password, verify_password
from app.auth.rate_limit import RateLimitExceeded, check_rate_limit
from app.auth.sessions import create_session, destroy_session
from app.config import get_settings
from app.db.base import get_db
from app.db.models.tenancy import Organization, OrganizationMember, OrgRole, User
from app.schemas.auth import LoginRequest, SignupRequest, UserOut

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
settings = get_settings()


def _set_session_cookie(response: Response, user_id: str) -> None:
    cookie_value = create_session(user_id)
    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=cookie_value,
        max_age=settings.SESSION_TTL_SECONDS,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        path="/",
    )


@router.post("/signup", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def signup(payload: SignupRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    try:
        check_rate_limit(f"signup:{client_ip}", settings.SIGNUP_ATTEMPTS_PER_HOUR_PER_IP, 3600)
    except RateLimitExceeded as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many signup attempts") from e

    existing = db.query(User).filter(User.email == payload.email.lower()).first()
    if existing is not None:
        # Same response whether the email is taken or malformed in some
        # other recoverable way — don't let this endpoint be used to
        # enumerate registered emails beyond what's unavoidable here.
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email may already exist")

    user = User(email=payload.email.lower(), password_hash=hash_password(payload.password))
    db.add(user)
    db.flush()

    org = Organization(name=payload.organization_name)
    db.add(org)
    db.flush()

    membership = OrganizationMember(
        organization_id=org.id, user_id=user.id, role=OrgRole.owner, joined_at=datetime.now(timezone.utc)
    )
    db.add(membership)
    db.commit()
    db.refresh(user)

    _set_session_cookie(response, str(user.id))
    return UserOut(id=str(user.id), email=user.email)


@router.post("/login", response_model=UserOut)
def login(payload: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    try:
        check_rate_limit(f"login:{client_ip}", settings.LOGIN_ATTEMPTS_PER_15MIN, 900)
        check_rate_limit(f"login:{payload.email.lower()}", settings.LOGIN_ATTEMPTS_PER_15MIN, 900)
    except RateLimitExceeded as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many login attempts") from e

    user = db.query(User).filter(User.email == payload.email.lower()).first()
    # Constant-shape failure path: run verify_password even on a missing
    # user (against a fixed dummy hash) so response timing doesn't reveal
    # whether the email exists.
    dummy_hash = "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$c29tZWhhc2g"
    password_ok = verify_password(payload.password, user.password_hash if user else dummy_hash)

    if user is None or not password_ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    user.last_login_at = datetime.now(timezone.utc)
    db.commit()

    _set_session_cookie(response, str(user.id))
    return UserOut(id=str(user.id), email=user.email)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response):
    cookie_value = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if cookie_value:
        destroy_session(cookie_value)
    response.delete_cookie(settings.SESSION_COOKIE_NAME, path="/")


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return UserOut(id=str(user.id), email=user.email)
