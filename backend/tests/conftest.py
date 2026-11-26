import pytest
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core import email as email_mod
from app.db.base import Base, SessionLocal, engine
from tests import email_safety
from tests.db_safety import UnsafeTestDatabase, check_test_database

# ---- EMAIL SAFETY (see tests/email_safety.py, docs/EMAIL_SAFETY.md). Runs at import, before any test, fixture or app code can
# send anything, and does not depend on ENV, on SMTP_HOST="" being exported, or on anyone remembering either. ----
email_mod.force_test_mode()  # send_email opens no SMTP connection in this process, whatever the settings say
_EMAIL_FIELDS_SCRUBBED = [f for f in ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD") if getattr(get_settings(), f, "")]
for _field in ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD"):
    setattr(get_settings(), _field, "")  # the real credentials from .env are not even readable by a test


def pytest_report_header(config):
    note = f"{len(_EMAIL_FIELDS_SCRUBBED)} SMTP setting(s) from the environment were scrubbed" if _EMAIL_FIELDS_SCRUBBED else "no SMTP settings were present"
    return f"email safety: real SMTP delivery is disabled for this run ({note})"


@pytest.fixture(scope="session", autouse=True)
def _no_real_smtp_connections():
    uninstall = email_safety.install()
    yield
    uninstall()


def _reset_quota_breaker():
    try:
        email_mod.clear_quota_pause()
    except (email_mod.BreakerStoreError, email_mod.StalePauseError):  # a test made the shared state unreadable, or raced a pause; it restores it itself
        email_mod._replace_local(None)


@pytest.fixture(autouse=True)
def _email_guard_per_test():
    """Fails any test whose code tried to reach a real mail server, even if the code swallowed the exception; clears the
    quota breaker and the record of refused sends so tests cannot affect each other."""
    email_safety.violations.clear()
    email_mod.blocked_in_test_mode.clear()
    _reset_quota_breaker()
    yield
    _reset_quota_breaker()
    attempted = list(email_safety.violations)
    email_safety.violations.clear()
    assert not attempted, f"a test tried to reach a real mail server: {attempted}"


class Outbox:
    """A local email catcher: what the app would have sent, with no network. `refuse` makes it pretend the server said no."""

    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []
        self.refuse: bool | set[str] = False

    def __call__(self, to, subject, body) -> bool:
        if self.refuse is True or (isinstance(self.refuse, set) and to in self.refuse):
            return False
        self.sent.append((to, subject, body))
        return True

    def to(self, address: str) -> list[tuple[str, str, str]]:
        return [m for m in self.sent if m[0] == address]

    def subjects(self) -> list[str]:
        return [m[1] for m in self.sent]


@pytest.fixture
def outbox():
    box = Outbox()
    previous = email_mod.install_test_transport(box)
    yield box
    email_mod.install_test_transport(previous)



@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    """
    Uses the same Postgres instance as the app (DATABASE_URL from .env)
    — these are real integration tests against a real database, not
    sqlite-in-memory approximations. Tables are created once per test
    session and torn down after.

    SAFETY GUARDRAIL: this fixture DROPS every table at teardown. Running
    it against a real dev/prod database is a real, recoverable-only-by-
    rerunning-migrations mistake — it happened twice already. Refuse to
    even start unless ENV, the host and the database name together make
    it obvious this is a local, throwaway test database (tests/db_safety.py:
    an exact name pattern, not a substring), rather than trusting every
    future `pytest` invocation to remember a special env var.
    """
    settings = get_settings()
    try:
        check_test_database(settings.DATABASE_URL, settings.ENV)
    except UnsafeTestDatabase as why:
        pytest.exit(
            f"\n\nREFUSING TO RUN: {why}\n"
            f"This test suite DROPS ALL TABLES at the end of the run - running it against a real dev or production database will wipe it.\n\n"
            f"Fix: createdb -O atla atla_test   (if you haven't already)\n"
            f"Then run tests as:\n"
            f'  ENV=test DATABASE_URL="postgresql+psycopg://atla:atla@localhost:5432/atla_test" python -m pytest tests/ -v\n',
            returncode=1,
        )
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db() -> Session:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    from app.config import get_settings
    import redis as redis_lib

    settings = get_settings()
    r = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
    for key in r.scan_iter("ratelimit:*"):
        r.delete(key)
    yield
    
class RecordingBillingProvider:
    """Stands in for PaddleBillingProvider inside the duplicate-subscription guard so tests never
    reach the real Paddle API. `cancelled` records (subscription_id, immediately) requests."""

    cancelled: list = []
    fail: bool = False

    def cancel_subscription(self, external_subscription_id, immediately=False):
        if type(self).fail:
            raise RuntimeError("paddle down")
        type(self).cancelled.append((external_subscription_id, immediately))


@pytest.fixture(autouse=True)
def _guard_never_calls_real_paddle(monkeypatch):
    from app.services import duplicate_subscriptions

    RecordingBillingProvider.cancelled = []
    RecordingBillingProvider.fail = False
    monkeypatch.setattr(duplicate_subscriptions, "PaddleBillingProvider", RecordingBillingProvider)
    yield RecordingBillingProvider

@pytest.fixture(autouse=True)
def _auto_provisioning_off_by_default(monkeypatch):
    """Existing tests must never reach the real Retell. test_auto_provisioning.py switches it on with a fake."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "AUTO_PROVISION_NUMBERS", False)
    yield
