"""
SANDBOX-ONLY, one-off: grant platform-admin (a row in `admin_users`) to ONE existing user.

    python -m app.workers.grant_sandbox_platform_admin --email you@example.com --confirm-database <db name>
    python -m app.workers.grant_sandbox_platform_admin --email you@example.com --confirm-database <db name> \
        --confirm-user-id <the id the first run printed>

Run it from a shell on the machine that holds the sandbox DATABASE_URL. It is deliberately NOT importable from the
API (nothing under app/api, app/services or app/main.py references it) and there is no HTTP route for it. It is a pilot tool,
not an admin-management system: it can only add, never remove, and it never touches `users.is_platform_admin`
(an unused column; `require_platform_admin` checks `admin_users` only).

It refuses to continue unless ALL of these hold; every refusal exits with code 2 and changes nothing:
  1. ENV is on an explicit allowlist (development, test) - "production" and anything unknown are refused - AND PADDLE_ENV is "sandbox".
  2. The DATABASE_URL can only reach this machine: the shared fail-closed check in app/db/url_safety.py looks at the host, ?host=,
     hostaddr, repeated or comma-separated hosts, socket directories and the PG* environment, and refuses anything it does not recognise.
  3. The database NAME matches an exact pattern - lower-case words joined by "_" whose LAST word is exactly "test" or "sandbox"
     (atla_test, atla_sandbox). A substring is not enough: atla_latest, atla_contest, atla_testing and atla_test_prod are refused.
  4. --confirm-database equals that name exactly. (The name check is one factor of several; it is never the only one.)
  5. --email matches exactly one user (compared case-insensitively; signup stores emails lower-cased).
  6. --confirm-user-id equals that user's id. Without it the tool only PRINTS the id it found and stops: granting is a second,
     deliberate step so the right account is checked with human eyes first.
Then, in ONE transaction: insert the `admin_users` row (only if none exists) and an `audit_logs` row. Re-running for an existing
admin changes nothing and writes no second audit row. Success is reported only if the admin row is confirmed to exist afterwards.
Exit codes: 0 granted or already admin; 2 refused (nothing changed); 1 failed BEFORE the commit (nothing was committed, so this run
granted nothing); 3 the commit SUCCEEDED but the check afterwards failed (the grant is most likely in place: inspect, do not retry
blindly); 4 the commit itself did not confirm (outcome unknown: inspect, do not retry blindly).
"""
import argparse
import logging
import re
import uuid

from sqlalchemy import func
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.base import SessionLocal
from app.db.models.platform import AdminUser, AuditLog
from app.db.models.tenancy import User
from app.db.url_safety import UnsafeDatabaseUrl, check_local_database_url

logger = logging.getLogger("atla.grant_sandbox_platform_admin")

AUDIT_ACTION = "platform_admin.granted"
# Explicit allowlists (not "anything that is not production"): a new ENV value or a remote host is refused until someone decides otherwise.
SAFE_ENVIRONMENTS = frozenset({"development", "test"})
SANDBOX_DATABASE_NAME = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*_(?:test|sandbox)$")


class Refused(Exception):
    """A safety check failed. The message is safe to show: it never contains a secret."""


class CommitOutcomeUnknown(Exception):
    """db.commit() raised. The server may or may not have applied the transaction (e.g. the connection dropped as it answered)."""

    def __init__(self, user_id: uuid.UUID):
        super().__init__(f"commit did not confirm for user {user_id}")
        self.user_id = user_id


class PostCommitVerificationFailed(Exception):
    """db.commit() SUCCEEDED, but the follow-up read could not confirm the admin row. This is NOT a rollback and NOT a known failure."""

    def __init__(self, user_id: uuid.UUID, reason: str):
        super().__init__(f"{reason} (user {user_id})")
        self.user_id = user_id
        self.reason = reason


def _database_url(settings: Settings):
    try:
        return make_url(settings.DATABASE_URL)
    except ArgumentError:
        raise Refused("Refusing: DATABASE_URL could not be parsed.") from None


def database_name(settings: Settings) -> str:
    """The database name from DATABASE_URL. Parsing only: no connection is opened."""
    return _database_url(settings).database or ""


def check_environment(settings: Settings, confirm_database: str | None) -> str:
    """Raises Refused unless this is clearly a local sandbox/test environment the operator named explicitly. Returns the db name."""
    if settings.is_production:
        raise Refused("Refusing: ENV=production. This tool is for sandbox/test environments only.")
    if settings.ENV not in SAFE_ENVIRONMENTS:
        raise Refused(f"Refusing: ENV={settings.ENV!r} is not on the safe list {sorted(SAFE_ENVIRONMENTS)}.")
    if settings.PADDLE_ENV != "sandbox":
        raise Refused(f"Refusing: PADDLE_ENV={settings.PADDLE_ENV}. Only PADDLE_ENV=sandbox is allowed.")
    try:  # host, hostaddr, ?host=, repeated/comma hosts, socket paths, PG* environment: one shared, fail-closed check (app/db/url_safety.py)
        url = check_local_database_url(settings.DATABASE_URL)
    except UnsafeDatabaseUrl as unsafe:
        raise Refused(f"Refusing: {unsafe}") from None
    name = url.database or ""
    if not SANDBOX_DATABASE_NAME.fullmatch(name.lower()):
        raise Refused(f"Refusing: database '{name}' is not a recognised sandbox/test database name "
                      "(its last '_'-separated word must be exactly 'test' or 'sandbox', e.g. atla_test).")
    if not confirm_database:
        raise Refused(f"Refusing: pass --confirm-database {name} to confirm this is the sandbox/test database you mean.")
    if confirm_database != name:
        raise Refused(f"Refusing: --confirm-database '{confirm_database}' does not match the connected database '{name}'.")
    return name


def find_exactly_one_user(db: Session, email: str) -> User:
    wanted = (email or "").strip().lower()
    if not wanted:
        raise Refused("Refusing: --email is required.")
    users = db.query(User).filter(func.lower(User.email) == wanted).all()
    if not users:
        raise Refused("Refusing: no user has that email. Nothing was changed.")
    if len(users) > 1:
        raise Refused(f"Refusing: {len(users)} users match that email; expected exactly one. Nothing was changed.")
    return users[0]


def _admin_exists(db: Session, user_id: uuid.UUID) -> bool:
    return db.query(AdminUser).filter(AdminUser.user_id == user_id).first() is not None


def _new_admin(user_id: uuid.UUID) -> AdminUser:
    return AdminUser(user_id=user_id)  # granted_by stays NULL: a one-off shell script has no acting user


def grant(db: Session, *, email: str, confirm_user_id: str | None, database: str, environment: str) -> tuple[str, uuid.UUID]:
    """
    Returns ("granted" | "already_admin", user id). Raises Refused for a failed safety check, and lets any database error propagate
    after rolling back: a rollback is NEVER reported as success. Commits only on a real grant.
    """
    user = find_exactly_one_user(db, email)
    user_id = user.id
    if not confirm_user_id:
        raise Refused(f"Found exactly one user: id={user_id}. Nothing was changed. To grant, re-run with --confirm-user-id {user_id}")
    try:
        confirmed = uuid.UUID(confirm_user_id.strip())
    except ValueError:
        raise Refused("Refusing: --confirm-user-id is not a valid UUID.") from None
    if confirmed != user_id:
        raise Refused(f"Refusing: --confirm-user-id does not match the user found for that email (found id={user_id}).")

    if _admin_exists(db, user_id):
        return "already_admin", user_id

    admin = _new_admin(user_id)
    db.add(admin)
    try:
        db.flush()  # the only place a concurrent grant for the same user can collide (unique user_id)
    except IntegrityError:
        db.rollback()
        if _admin_exists(db, user_id):  # a genuine concurrent grant won; it committed the admin row AND its audit row together
            return "already_admin", user_id
        raise  # an integrity failure that is NOT "this user already became admin": nothing was granted, so say so

    # Stage 1 - BEFORE the commit. Any failure here means nothing was committed: roll back and let the original error propagate.
    try:
        db.add(AuditLog(
            organization_id=None, actor_user_id=None, action=AUDIT_ACTION, target_type="admin_user", target_id=str(admin.id),
            audit_metadata={"user_id": str(user_id), "via": "grant_sandbox_platform_admin", "database": database, "environment": environment},
        ))
        db.flush()  # an integrity error on the audit row surfaces HERE, before the commit; it is never "already_admin"
    except Exception:
        db.rollback()
        raise

    # Stage 2 - the commit itself (admin row + audit row, one transaction). If it raises we cannot know whether the server applied it.
    try:
        db.commit()
    except Exception as exc:
        try:
            db.rollback()
        except Exception:  # best effort: the connection may be gone
            pass
        raise CommitOutcomeUnknown(user_id) from exc

    # Stage 3 - AFTER a successful commit. A failure here says nothing about whether the grant was applied: it was committed.
    try:
        present = _admin_exists(db, user_id)
    except Exception as exc:
        raise PostCommitVerificationFailed(user_id, f"the verification query failed: {type(exc).__name__}") from exc
    if not present:
        raise PostCommitVerificationFailed(user_id, "the commit succeeded but the admin row was not found afterwards")
    try:
        logger.warning("Platform admin granted to user %s in database %s", user_id, database)
    except Exception:  # the grant is committed and verified; a logging problem must not change that
        pass
    return "granted", user_id


def _close_quietly(db: Session) -> bool:
    """
    Closes the session without letting a cleanup failure change what is reported. By the time this runs the transaction's outcome is
    already decided (committed, refused or failed), and closing the session cannot alter it. Returns False if closing failed.
    """
    try:
        db.close()
    except Exception:
        try:
            logger.warning("Closing the database session failed; the grant outcome is unaffected", exc_info=True)
        except Exception:  # even a broken logger must not escape: the outcome is already decided
            pass
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SANDBOX-ONLY: grant platform-admin to one existing user.")
    parser.add_argument("--email", required=True, help="exact email of the existing user")
    parser.add_argument("--confirm-database", help="the database name from DATABASE_URL, typed explicitly")
    parser.add_argument("--confirm-user-id", help="the user id this tool printed for that email; required to actually grant")
    args = parser.parse_args(argv)

    settings = get_settings()
    session_closed = True
    try:
        name = check_environment(settings, args.confirm_database)  # every environment check passes BEFORE any database session is opened
        db = SessionLocal()
        try:
            outcome, user_id = grant(db, email=args.email, confirm_user_id=args.confirm_user_id, database=name, environment=settings.ENV)
        finally:
            session_closed = _close_quietly(db)
    except Refused as refusal:
        print(str(refusal))
        return 2
    except PostCommitVerificationFailed as failure:
        logger.exception("Platform admin commit succeeded but could not be verified")
        print(f"COMMIT SUCCEEDED, VERIFICATION FAILED: {failure.reason}.\n"
              "The database accepted the commit, so the admin grant and its audit row are most likely IN PLACE. This is not a rollback.\n"
              "Do NOT re-run this tool blindly. Inspect first:\n"
              f"  SELECT * FROM admin_users WHERE user_id = '{failure.user_id}';\n"
              f"  SELECT * FROM audit_logs WHERE action = '{AUDIT_ACTION}' AND audit_metadata->>'user_id' = '{failure.user_id}';")
        return 3
    except CommitOutcomeUnknown as failure:
        logger.exception("Platform admin commit did not confirm")
        print("COMMIT OUTCOME UNKNOWN: the database did not confirm the commit.\n"
              "The grant may or may not have been applied. Do NOT re-run this tool blindly. Inspect first:\n"
              f"  SELECT * FROM admin_users WHERE user_id = '{failure.user_id}';\n"
              f"  SELECT * FROM audit_logs WHERE action = '{AUDIT_ACTION}' AND audit_metadata->>'user_id' = '{failure.user_id}';")
        return 4
    except (SQLAlchemyError, RuntimeError) as failure:
        logger.exception("Granting platform admin failed")
        print(f"FAILED ({type(failure).__name__}) before anything was committed: NO admin was granted by this run. "
              "See the log for details.")
        return 1
    except Exception as failure:
        # Anything else that reaches here happened BEFORE the commit: grant() turns every failure at or after the commit into
        # CommitOutcomeUnknown / PostCommitVerificationFailed, handled above. So this is still "nothing committed", never a guess about a commit.
        logger.exception("Granting platform admin failed unexpectedly")
        print(f"UNEXPECTED ERROR ({type(failure).__name__}) before anything was committed: NO admin was granted by this run. "
              "See the log for details.")
        return 1

    if outcome == "already_admin":
        print(f"User {user_id} is already a platform admin. Nothing was changed.")
    else:
        print(f"Granted platform admin to user {user_id} in database '{name}'. Audit row written ({AUDIT_ACTION}).")
    if not session_closed:
        print("Note: closing the database session failed afterwards. The outcome above is unaffected; see the log.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
