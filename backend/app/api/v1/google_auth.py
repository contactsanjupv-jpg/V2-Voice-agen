"""Continue with Google. See docs/GOOGLE_SIGN_IN.md.

Account policy (the part that matters for security):
  * A returning Google user is matched ONLY by Google's stable `sub`.
  * A matching EMAIL is never proof of ownership: Google's `email_verified` says the address was verified when that Google
    account was made, not that the person controls the mailbox today (reassigned/recycled addresses, departed employees).
    So an email match never signs anyone in, never links anything, and never changes a password or a session.
  * An existing Atla account is connected to Google only after proof that the person owns the ATLA account:
      - the account has a password  -> the person enters it (POST /google/link/confirm);
      - no password, email never verified -> they click a link emailed to the account's address, in the same browser
        (POST /google/link/email, /google/link/verify);
      - no password, email already verified (e.g. it belongs to another Google account) -> refused.
      - already signed in -> explicit "connect Google" from Settings (POST /google/link/start): the password is re-entered if the
        account has one; a passwordless account must prove the mailbox with a one-time emailed link bound to the user, the
        session, the browser and the Google state (POST /google/link/verify-start). A session alone is never enough.
  * A brand-new person (no Atla account for that email) gets one, after naming their business (POST /google/complete).
Redirect targets are fixed (FRONTEND_URL + a path); nothing the browser sends can choose where we redirect.
"""
import hmac
import logging
import secrets
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.v1.account import _deliver
from app.api.v1.auth import _set_session_cookie
from app.auth import google
from app.auth.deps import get_current_user
from app.auth.google import GoogleAuthError, GoogleIdentity
from app.auth.passwords import verify_password
from app.auth.rate_limit import RateLimitExceeded, check_rate_limit
from app.auth.sessions import read_session
from app.config import get_settings
from app.db.base import get_db
from app.db.models.tenancy import OAuthIdentity, Organization, OrganizationMember, OrgRole, User
from app.schemas.auth import UserOut

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
settings = get_settings()
logger = logging.getLogger("atla.google")

PROVIDER = "google"
STATE_COOKIE = "atla_g_state"
PENDING_COOKIE = "atla_g_pending"
BIND_COOKIE = "atla_g_linkbind"
COOKIE_PATH = "/api/v1/auth/google"
SETTINGS_PATH = "/dashboard/settings"


class CompleteRequest(BaseModel):
    organization_name: str = Field(min_length=1, max_length=255)


class PasswordRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class OptionalPasswordRequest(BaseModel):
    password: str | None = Field(default=None, max_length=256)


class VerifyRequest(BaseModel):
    token: str = Field(min_length=10, max_length=200)


class LinkIntentRequest(BaseModel):
    token: str = Field(min_length=10, max_length=200)


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _limit(key: str, limit: int, window: int, message: str = "Too many attempts. Please try again later.") -> None:
    try:
        check_rate_limit(key, limit, window)
    except RateLimitExceeded as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, message) from e


def _cookie_kwargs(max_age: int) -> dict:
    return dict(max_age=max_age, httponly=True, secure=settings.is_production, samesite="lax", path=COOKIE_PATH)


def _back(error: str, link: bool = False) -> RedirectResponse:
    """To the login page (or Settings, for an explicit link) with a short code the frontend maps to a message."""
    target = f"{SETTINGS_PATH}?google_link={quote(error)}" if link else f"/get-started?google_error={quote(error)}"
    resp = RedirectResponse(f"{settings.FRONTEND_URL}{target}", status_code=302)
    resp.delete_cookie(STATE_COOKIE, path=COOKIE_PATH)
    return resp


# ---------------- what is possible, for the UI ----------------

@router.get("/providers")
def providers():
    """Which sign-in methods are really usable, so the UI never shows a button that cannot work."""
    return {"google": settings.google_configured}


@router.get("/google/status")
def google_status(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    linked = db.query(OAuthIdentity).filter(OAuthIdentity.user_id == user.id, OAuthIdentity.provider == PROVIDER).first()
    return {"configured": settings.google_configured, "linked": linked is not None, "has_password": user.password_hash is not None}


# ---------------- start ----------------

@router.get("/google/start")
def google_start(request: Request):
    if not settings.google_configured:
        logger.error("Google sign-in was requested but GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET/GOOGLE_REDIRECT_URI are not all set")
        return _back("not_configured")
    try:
        check_rate_limit(f"google-start:{_ip(request)}", 30, 900)
    except RateLimitExceeded:
        return _back("rate_limited")
    state, url = google.begin_authorization()
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(STATE_COOKIE, state, **_cookie_kwargs(google.STATE_TTL_SECONDS))
    return resp


def _same(a: str | None, b: str | None) -> bool:
    return bool(a and b) and hmac.compare_digest(a, b)


@router.post("/google/link/start")
def google_link_start(
    payload: OptionalPasswordRequest,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Explicit "Connect Google" by a signed-in user. A session alone is never enough to attach a Google account:

      * account WITH a password -> the current password must be entered now; the response carries the Google URL;
      * account WITHOUT a password -> proof of the mailbox: a one-time link is emailed to the account's own address and the
        response carries NO URL. The link must be opened in this same browser and signed-in session (/google/link/verify-start).
    """
    if not settings.google_configured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Google sign-in isn't set up on this server.")
    _limit(f"google-link-start:{user.id}", 5, 900)
    if db.query(OAuthIdentity).filter(OAuthIdentity.user_id == user.id, OAuthIdentity.provider == PROVIDER).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "A Google account is already connected.")
    session_digest = google.digest(request.cookies.get(settings.SESSION_COOKIE_NAME, ""))

    if user.password_hash is not None:
        if not payload.password or not verify_password(payload.password, user.password_hash):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "That password isn't correct.")
        state, url = google.begin_authorization(link_user_id=str(user.id), link_session=session_digest)
        response.set_cookie(STATE_COOKIE, state, **_cookie_kwargs(google.STATE_TTL_SECONDS))
        return {"url": url}

    _limit(f"google-link-mail:{user.id}", 3, 3600)
    _limit(f"google-link-mail-ip:{_ip(request)}", 10, 3600)
    bind = secrets.token_urlsafe(32)  # stays in THIS browser; the emailed link is useless anywhere else
    token = google.save_link_intent(str(user.id), session_digest, google.digest(bind))
    response.set_cookie(BIND_COOKIE, bind, **_cookie_kwargs(google.LINK_INTENT_TTL_SECONDS))
    link = f"{settings.FRONTEND_URL}{SETTINGS_PATH}?google_link_verify={token}"
    background.add_task(
        _deliver,
        user.email,
        "Confirm connecting Google to your Atla account",
        "Someone signed in to your Atla account asked to connect a Google account to it.\n\n"
        f"If that was you, open this link in the SAME browser where you clicked Connect Google (valid for "
        f"{google.LINK_INTENT_TTL_SECONDS // 60} minutes, works once):\n\n{link}\n\n"
        "If it wasn't you, don't open it: nothing is connected, and the link is useless in any other browser. "
        "Sign out of Atla on any device you don't recognize.",
    )
    return {"verification": "email_sent"}


@router.post("/google/link/verify-start")
def google_link_verify_start(
    payload: LinkIntentRequest,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    bind_cookie: str | None = Cookie(default=None, alias=BIND_COOKIE),
):
    """Second half of the passwordless path. Succeeds only for the SAME user, in the SAME signed-in session and the SAME browser
    that asked, with a token that is unused and unexpired. It then creates the Google state (single use, browser-bound)."""
    _limit(f"google-link-verify-ip:{_ip(request)}", 20, 900)
    invalid = HTTPException(status.HTTP_400_BAD_REQUEST, "This link is invalid or has expired. Please start again from Settings.")
    session_digest = google.digest(request.cookies.get(settings.SESSION_COOKIE_NAME, ""))
    intent = google.peek_link_intent(payload.token)
    if (
        intent is None
        or intent["user_id"] != str(user.id)
        or not _same(intent["session"], session_digest)
        or not bind_cookie
        or not _same(intent["bind"], google.digest(bind_cookie))
    ):
        raise invalid  # nothing is consumed on a mismatch: a stranger holding a copied link can't burn the real one
    if user.password_hash is not None:  # it gained a password since: that account must use the password path
        raise HTTPException(status.HTTP_409_CONFLICT, "This account has a password now. Connect Google with your password instead.")
    if db.query(OAuthIdentity).filter(OAuthIdentity.user_id == user.id, OAuthIdentity.provider == PROVIDER).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "A Google account is already connected.")
    if google.consume_link_intent(payload.token) is None:  # atomic: two simultaneous uses, one wins
        raise invalid
    state, url = google.begin_authorization(link_user_id=str(user.id), link_session=session_digest)
    response.set_cookie(STATE_COOKIE, state, **_cookie_kwargs(google.STATE_TTL_SECONDS))
    response.delete_cookie(BIND_COOKIE, path=COOKIE_PATH)
    return {"url": url}


# ---------------- shared helpers ----------------

class _Conflict(Exception):
    """Why a Google identity could not be attached. `reason` is "other_user" (this Google account already belongs to a
    different Atla user) or "user_has_google" (this Atla user already has a different Google account)."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


_CONFLICT_TEXT = {
    "other_user": "This Google account is already connected to a different Atla account.",
    "user_has_google": "A different Google account is already connected to this Atla account.",
}


def _conflict_http(e: _Conflict) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, _CONFLICT_TEXT[e.reason])


def _identity_for_subject(db: Session, subject: str) -> OAuthIdentity | None:
    return db.query(OAuthIdentity).filter(OAuthIdentity.provider == PROVIDER, OAuthIdentity.subject == subject).first()


def _identity_for_user(db: Session, user_id) -> OAuthIdentity | None:
    return db.query(OAuthIdentity).filter(OAuthIdentity.user_id == user_id, OAuthIdentity.provider == PROVIDER).first()


def _attach(db: Session, user: User, ident: GoogleIdentity) -> None:
    """Bind the Google identity to the user. Touches nothing else: not the password, not other sessions, not the email.
    One Google account per Atla user and one Atla user per Google account. The checks give clear answers; the database
    constraints (uq_oauth_provider_subject, uq_oauth_user_provider) are what make them hold when requests race."""
    user_id = user.id
    existing = _identity_for_subject(db, ident.sub)
    if existing is not None:
        if existing.user_id != user_id:
            raise _Conflict("other_user")
        return
    mine = _identity_for_user(db, user_id)
    if mine is not None:
        if mine.subject == ident.sub:
            return
        raise _Conflict("user_has_google")
    try:
        db.add(OAuthIdentity(user_id=user_id, provider=PROVIDER, subject=ident.sub, email=ident.email))
        db.commit()
    except IntegrityError:  # two requests raced past the checks above; the database let exactly one through
        db.rollback()
        winner = _identity_for_subject(db, ident.sub)
        if winner is not None:
            if winner.user_id == user_id:
                return  # the same identity, attached by a parallel request: nothing more to do
            raise _Conflict("other_user") from None
        raise _Conflict("user_has_google") from None  # no row for this subject, so the (user, provider) constraint refused it


def _link_alert(user_email: str) -> tuple[str, str, str]:
    return (
        user_email,
        "Google sign-in was added to your Atla account",
        "A Google account was just connected to your Atla account, so you can now sign in with it. Your password and your "
        "other signed-in devices were not changed."
        f"\n\nIf this wasn't you, reset your password right away: {settings.FRONTEND_URL}/forgot-password",
    )


def _pending_kind(db: Session, ident: GoogleIdentity) -> tuple[str, User | None]:
    """What a verified Google identity with no Atla link yet may do next. Looks the account up by email, but an email match
    only decides WHICH proof is asked for; it proves nothing by itself."""
    user = db.query(User).filter(User.email == ident.email).first()
    if user is None:
        return "new", None
    if user.password_hash is not None:
        return "link_password", user
    if user.email_verified_at is None:
        return "link_email", user
    return "blocked", user  # verified, no password: it belongs to another Google account; an email match can't take it over


def _login_response(user: User, response: Response | RedirectResponse) -> None:
    _set_session_cookie(response, str(user.id))  # a brand-new session id; every other session is left alone
    response.delete_cookie(PENDING_COOKIE, path=COOKIE_PATH)
    response.delete_cookie(STATE_COOKIE, path=COOKIE_PATH)


def _pending_identity(pending: str | None) -> GoogleIdentity:
    ident = google.peek_pending(pending) if pending else None
    if ident is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This sign-in has expired. Please continue with Google again.")
    return ident


# ---------------- callback ----------------

@router.get("/google/callback")
def google_callback(
    request: Request,
    background: BackgroundTasks,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    state_cookie: str | None = Cookie(default=None, alias=STATE_COOKIE),
    db: Session = Depends(get_db),
):
    if not settings.google_configured:
        return _back("not_configured")
    try:
        check_rate_limit(f"google-callback:{_ip(request)}", 60, 900)
    except RateLimitExceeded:
        return _back("rate_limited")

    # 1. The state must be the one issued to THIS browser (cookie) and still unused (Redis, single use).
    stored = None
    if google.states_match(state_cookie, state):
        stored = google.consume_state(state)
    if stored is None:
        logger.warning("Google sign-in rejected: state missing, mismatched, expired or replayed")
        return _back("expired")
    link_user_id = stored.get("link_user_id")
    linking = link_user_id is not None

    # 2. The person said no (or Google refused). Nothing is created or changed.
    if error:
        logger.info("Google sign-in ended by the user or Google: %s", "cancelled" if error == "access_denied" else "error")
        return _back("cancelled" if error == "access_denied" else "failed", linking)
    if not code:
        return _back("failed", linking)

    # 3. Code -> ID token -> validated identity.
    try:
        ident = google.verify_id_token(google.exchange_code(code, stored["verifier"]), stored["nonce"])
    except GoogleAuthError as exc:
        logger.warning("Google sign-in rejected: %s", exc.code)
        return _back("unverified_email" if exc.code in ("email_not_verified", "no_email") else "failed", linking)

    # 4a. Explicit link by a signed-in user: the session at the callback must still be the user who started it.
    if linking:
        raw_session = request.cookies.get(settings.SESSION_COOKIE_NAME, "")
        session = read_session(raw_session)
        if session is None or session["user_id"] != link_user_id or not _same(stored.get("link_session"), google.digest(raw_session)):
            return _back("session_changed", True)  # a different user, or a different session than the one that asked
        user = db.get(User, uuid.UUID(link_user_id))
        try:
            _attach(db, user, ident)
        except _Conflict as e:
            return _back("already_linked" if e.reason == "other_user" else "already_connected", True)
        background.add_task(_deliver, *_link_alert(user.email))
        return _back("ok", True)

    # 4b. Returning Google user: matched by `sub` and nothing else.
    linked = db.query(OAuthIdentity).filter(OAuthIdentity.provider == PROVIDER, OAuthIdentity.subject == ident.sub).first()
    if linked is not None:
        user = db.get(User, linked.user_id)
        user.last_login_at = datetime.now(timezone.utc)
        db.commit()
        resp = RedirectResponse(f"{settings.FRONTEND_URL}/onboarding", status_code=302)
        _login_response(user, resp)
        return resp

    # 4c. Nobody is signed in and nothing is linked. Whether this is a new person or an existing account, the next step is
    #     chosen by /google/pending (the identity waits server-side, tied to this browser); no session, no change.
    resp = RedirectResponse(f"{settings.FRONTEND_URL}/get-started/google", status_code=302)
    resp.set_cookie(PENDING_COOKIE, google.save_pending(ident), **_cookie_kwargs(google.PENDING_TTL_SECONDS))
    resp.delete_cookie(STATE_COOKIE, path=COOKIE_PATH)
    return resp


# ---------------- pending identity: new person, or proving ownership of an existing account ----------------

@router.get("/google/pending")
def google_pending(pending: str | None = Cookie(default=None, alias=PENDING_COOKIE), db: Session = Depends(get_db)):
    ident = google.peek_pending(pending) if pending else None
    if ident is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This sign-in has expired. Please start again.")
    kind, _user = _pending_kind(db, ident)
    return {"email": ident.email, "name": ident.name, "kind": kind}


@router.post("/google/complete", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def google_complete(
    payload: CompleteRequest,
    request: Request,
    response: Response,
    pending: str | None = Cookie(default=None, alias=PENDING_COOKIE),
    db: Session = Depends(get_db),
):
    """Brand-new person only. If an Atla account for that email exists, this refuses: it can never be used to take it over."""
    _limit(f"google-complete:{_ip(request)}", 20, 900)
    name = payload.organization_name.strip()
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Enter your business name.")
    ident = _pending_identity(pending)
    kind, _ = _pending_kind(db, ident)
    if kind != "new":
        raise HTTPException(status.HTTP_409_CONFLICT, "An Atla account already exists for this email. Confirm it's yours to connect Google.")
    if google.consume_pending(pending) is None:  # single use, even under concurrent calls
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This sign-in has expired. Please continue with Google again.")
    now = datetime.now(timezone.utc)
    try:
        # email_verified_at records that Google vouched for the address at sign-up. It is only ever used to REFUSE later
        # email-based linking, never to allow it.
        user = User(email=ident.email, password_hash=None, email_verified_at=now, last_login_at=now)
        db.add(user)
        db.flush()
        org = Organization(name=name)
        db.add(org)
        db.flush()
        db.add(OrganizationMember(organization_id=org.id, user_id=user.id, role=OrgRole.owner, joined_at=now))
        db.add(OAuthIdentity(user_id=user.id, provider=PROVIDER, subject=ident.sub, email=ident.email))
        db.commit()
    except IntegrityError as e:  # the email or the Google account appeared in the meantime
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "An account already exists for this email. Please continue with Google again.") from e
    _login_response(user, response)
    return UserOut(id=str(user.id), email=user.email)


@router.post("/google/link/confirm", response_model=UserOut)
def google_link_confirm(
    payload: PasswordRequest,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    pending: str | None = Cookie(default=None, alias=PENDING_COOKIE),
    db: Session = Depends(get_db),
):
    """Existing account WITH a password: connect Google only after the person proves they know it."""
    _limit(f"google-link-ip:{_ip(request)}", 20, 900)
    ident = _pending_identity(pending)
    kind, user = _pending_kind(db, ident)
    if kind != "link_password":
        raise HTTPException(status.HTTP_409_CONFLICT, "This account can't be connected with a password.")
    _limit(f"google-link-pw:{user.id}", 5, 900, "Too many incorrect passwords. Please wait a few minutes, or reset your password.")
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "That password isn't correct.")  # the pending sign-in stays valid for a retry
    # Atomic single use (GETDEL), and only after the password is right, so a wrong password never spends the sign-in. Of
    # concurrent requests holding this pending identity and the right password, exactly one gets it back; the rest are
    # refused before anything is attached, no session is created and no alert is sent.
    spent = google.consume_pending(pending)
    if spent is None or spent != ident:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This sign-in has expired. Please continue with Google again.")
    try:
        _attach(db, user, ident)
    except _Conflict as e:
        raise _conflict_http(e) from e
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    background.add_task(_deliver, *_link_alert(user.email))
    _login_response(user, response)
    return UserOut(id=str(user.id), email=user.email)


@router.post("/google/link/email", status_code=status.HTTP_204_NO_CONTENT)
def google_link_email(
    request: Request,
    background: BackgroundTasks,
    pending: str | None = Cookie(default=None, alias=PENDING_COOKIE),
    db: Session = Depends(get_db),
):
    """Existing account with no password and an unverified email: prove mailbox ownership with a link sent to that address."""
    _limit(f"google-linkmail-ip:{_ip(request)}", 10, 3600)
    ident = _pending_identity(pending)
    kind, user = _pending_kind(db, ident)
    if kind != "link_email":
        raise HTTPException(status.HTTP_409_CONFLICT, "This account doesn't need email verification.")
    _limit(f"google-linkmail-user:{user.id}", 3, 3600)
    token = google.save_link_verification(pending, str(user.id))
    link = f"{settings.FRONTEND_URL}/get-started/google?verify={token}"
    background.add_task(
        _deliver,
        user.email,
        "Confirm connecting Google to your Atla account",
        f"Someone signed in with a Google account and asked to connect it to your Atla account.\n\n"
        f"If that was you, open this link in the SAME browser where you started (valid for "
        f"{google.LINK_VERIFY_TTL_SECONDS // 60} minutes):\n\n{link}\n\nIf it wasn't you, ignore this email: nothing is connected.",
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/google/link/verify", response_model=UserOut)
def google_link_verify(
    payload: VerifyRequest,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    pending: str | None = Cookie(default=None, alias=PENDING_COOKIE),
    db: Session = Depends(get_db),
):
    """Needs BOTH the emailed token (proves the mailbox) and this browser's pending cookie (proves the Google sign-in)."""
    _limit(f"google-linkverify-ip:{_ip(request)}", 20, 900)
    invalid = HTTPException(status.HTTP_400_BAD_REQUEST, "This link is invalid or has expired. Please start again.")
    ident = _pending_identity(pending)
    kind, user = _pending_kind(db, ident)
    # Cheap pre-check on a PEEK, so someone holding only a copied link (wrong browser) is turned away without spending it.
    # It is only a filter: every decision below is made on what the ATOMIC consumption actually returns.
    peeked = google.peek_link_verification(payload.token)
    if peeked is None or kind != "link_email" or peeked["pending"] != google.pending_digest(pending) or peeked["user_id"] != str(user.id):
        raise invalid
    # Atomic single use. Concurrent or replayed requests all pass the filter above, but exactly one gets the record here.
    record = google.consume_link_verification(payload.token)
    if record is None or record["pending"] != google.pending_digest(pending) or record["user_id"] != str(user.id):
        raise invalid
    # The Google identity waiting in this browser is spent the same way, and must be the very one we checked.
    spent = google.consume_pending(pending)
    if spent is None or spent != ident:
        raise invalid
    try:
        _attach(db, user, ident)
    except _Conflict as e:
        raise _conflict_http(e) from e
    user.last_login_at = datetime.now(timezone.utc)  # linking never alters the account's verification state
    db.commit()
    background.add_task(_deliver, *_link_alert(user.email))
    _login_response(user, response)
    return UserOut(id=str(user.id), email=user.email)
