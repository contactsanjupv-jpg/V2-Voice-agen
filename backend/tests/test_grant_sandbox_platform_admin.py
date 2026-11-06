"""
The sandbox-only platform-admin grant script (app/workers/grant_sandbox_platform_admin.py).

These tests run its real `main()` against the throwaway TEST database (the suite refuses to start on anything else) and
only ever touch users they create. In every refusal case `SessionLocal` is replaced by a trap that fails the test if a
database session is opened, which proves the environment checks happen BEFORE any database work.
"""
import threading
import uuid
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError, OperationalError

from app.config import get_settings
from app.db.base import SessionLocal as RealSessionLocal
from app.db.models.platform import AdminUser, AuditLog
from app.db.models.tenancy import User
from app.main import app
from app.workers import grant_sandbox_platform_admin as script
from tests.test_tenant_isolation import _signup

ROOT = Path(__file__).resolve().parents[1]


def _db_name() -> str:
    return script.database_name(get_settings())


def _new_user():
    tag = uuid.uuid4().hex[:10]
    email = f"grant-{tag}@sandbox-admin-test.com"
    client, _org = _signup(email, f"Grant {tag}")
    db = RealSessionLocal()
    try:
        return client, email, db.query(User).filter(User.email == email).one().id
    finally:
        db.close()


def _admin_rows(user_id) -> int:
    db = RealSessionLocal()
    try:
        return db.query(AdminUser).filter(AdminUser.user_id == user_id).count()
    finally:
        db.close()


def _audit_rows(user_id) -> list[AuditLog]:
    db = RealSessionLocal()
    try:
        rows = db.query(AuditLog).filter(AuditLog.action == script.AUDIT_ACTION, AuditLog.audit_metadata["user_id"].astext == str(user_id)).all()
        for r in rows:
            db.expunge(r)
        return rows
    finally:
        db.close()


def _total_admins() -> int:
    db = RealSessionLocal()
    try:
        return db.query(func.count(AdminUser.id)).scalar()
    finally:
        db.close()


def _run(*, email, confirm_db="__use_real__", confirm_id=None):
    argv = ["--email", email]
    if confirm_db is not None:
        argv += ["--confirm-database", _db_name() if confirm_db == "__use_real__" else confirm_db]
    if confirm_id is not None:
        argv += ["--confirm-user-id", str(confirm_id)]
    return script.main(argv)


@pytest.fixture
def no_database(monkeypatch):
    """Any attempt to open a database session fails the test: the refusal must come first."""
    def trap():
        raise AssertionError("a database session was opened before the safety checks finished")

    monkeypatch.setattr(script, "SessionLocal", trap)


# ---------------- production refusal ----------------

def test_production_is_refused_before_any_database_work(monkeypatch, no_database, capsys):
    _client, email, user_id = _new_user()
    monkeypatch.setattr(get_settings(), "ENV", "production")
    assert _run(email=email, confirm_id=user_id) == 2
    assert "ENV=production" in capsys.readouterr().out
    assert _admin_rows(user_id) == 0 and _audit_rows(user_id) == []


def test_live_billing_is_refused_too(monkeypatch, no_database, capsys):
    _client, email, user_id = _new_user()
    monkeypatch.setattr(get_settings(), "PADDLE_ENV", "live")
    assert _run(email=email, confirm_id=user_id) == 2
    assert "PADDLE_ENV=live" in capsys.readouterr().out
    assert _admin_rows(user_id) == 0


# ---------------- wrong / missing database confirmation ----------------

def test_a_missing_database_confirmation_is_refused(no_database, capsys):
    _client, email, user_id = _new_user()
    assert _run(email=email, confirm_db=None, confirm_id=user_id) == 2
    assert f"--confirm-database {_db_name()}" in capsys.readouterr().out
    assert _admin_rows(user_id) == 0


def test_a_database_confirmation_that_does_not_match_is_refused(no_database, capsys):
    _client, email, user_id = _new_user()
    assert _run(email=email, confirm_db="some_other_sandbox_db", confirm_id=user_id) == 2
    assert "does not match" in capsys.readouterr().out
    assert _admin_rows(user_id) == 0


def _url_with(monkeypatch, **parts) -> str:
    """Point the (singleton) settings at a different DATABASE_URL. Nothing connects: the checks only parse it."""
    url = make_url(get_settings().DATABASE_URL).set(**parts)
    monkeypatch.setattr(get_settings(), "DATABASE_URL", url.render_as_string(hide_password=False))
    return url.database


def _settings(**update):
    return get_settings().model_copy(update=update)


def test_a_database_that_does_not_look_like_a_sandbox_is_refused_even_if_confirmed(monkeypatch, no_database, capsys):
    _client, email, user_id = _new_user()
    name = _url_with(monkeypatch, database="atla_main")
    assert _run(email=email, confirm_db=name, confirm_id=user_id) == 2
    assert "not a recognised sandbox/test database name" in capsys.readouterr().out
    assert _admin_rows(user_id) == 0


@pytest.mark.parametrize("name", ["atla_test", "atla_sandbox", "atla_dev_test", "myapp_staging_sandbox", "atla2_test"])
def test_the_intended_sandbox_database_names_are_accepted(name):
    settings = _settings(DATABASE_URL=f"postgresql+psycopg://atla:atla@localhost:5432/{name}", ENV="development")
    assert script.check_environment(settings, name) == name  # atla_test (CI and local) must keep working


@pytest.mark.parametrize("name", [
    "atla_latest",      # contains "test" as a SUBSTRING: the bug this check replaces
    "atla_contest", "atla_testing", "atla_tests", "atla_test_prod", "atla_test2", "atla_attest", "latest_prod",
    "atla", "atla_prod", "atla_production", "test", "sandbox", "_test", "atla-test", "atla test", "atla_Test_x1",
    "atla_sandboxed", "sandbox_atla",
])
def test_names_that_merely_contain_test_or_sandbox_are_refused_even_when_confirmed(name):
    settings = _settings(DATABASE_URL=f"postgresql+psycopg://atla:atla@localhost:5432/{name}", ENV="development")
    with pytest.raises(script.Refused, match="not a recognised sandbox/test database name"):
        script.check_environment(settings, name)


def test_atla_latest_is_refused_through_main_without_opening_a_database_session(monkeypatch, no_database, capsys):
    _client, email, user_id = _new_user()
    name = _url_with(monkeypatch, database="atla_latest")
    assert _run(email=email, confirm_db=name, confirm_id=user_id) == 2
    assert "atla_latest" in capsys.readouterr().out
    assert _admin_rows(user_id) == 0


def test_only_the_explicit_environment_allowlist_is_accepted():
    url = "postgresql+psycopg://atla:atla@localhost:5432/atla_test"
    assert script.SAFE_ENVIRONMENTS == {"development", "test"}
    for env in ("development", "test"):
        assert script.check_environment(_settings(DATABASE_URL=url, ENV=env), "atla_test") == "atla_test"
    for env in ("production", "staging", "prod", "Production", ""):  # anything not on the list, including values Settings would reject
        with pytest.raises(script.Refused):
            script.check_environment(_settings(DATABASE_URL=url, ENV=env), "atla_test")


def test_billing_must_be_sandbox_and_a_name_confirmation_alone_cannot_override_it():
    url = "postgresql+psycopg://atla:atla@localhost:5432/atla_test"
    with pytest.raises(script.Refused, match="PADDLE_ENV=live"):
        script.check_environment(_settings(DATABASE_URL=url, ENV="test", PADDLE_ENV="live"), "atla_test")


@pytest.mark.parametrize("host", ["prod-db.example.com", "10.0.0.5", "db.internal", "LOCALHOST.evil.com", "127.0.0.1.evil.com"])
def test_a_non_local_database_host_is_refused_even_with_a_sandbox_looking_name(monkeypatch, no_database, capsys, host):
    _client, email, user_id = _new_user()
    name = _url_with(monkeypatch, host=host, database="atla_test")
    assert _run(email=email, confirm_db=name, confirm_id=user_id) == 2
    assert "is not local" in capsys.readouterr().out
    assert _admin_rows(user_id) == 0


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "LOCALHOST", None])
def test_local_hosts_and_local_sockets_are_accepted(host):
    url = URL.create("postgresql+psycopg", username="atla", password="atla", host=host, database="atla_test")  # host=None: a local socket
    settings = _settings(DATABASE_URL=url.render_as_string(hide_password=False), ENV="test")
    assert script.check_environment(settings, "atla_test") == "atla_test"


def test_an_unparsable_database_url_is_refused_not_a_crash():
    with pytest.raises(script.Refused, match="could not be parsed"):
        script.check_environment(_settings(DATABASE_URL="not a url", ENV="test"), "x_test")


# ---------------- the target user ----------------

def test_a_missing_user_is_refused_and_nothing_is_created(capsys):
    before = _total_admins()
    assert _run(email=f"nobody-{uuid.uuid4().hex[:8]}@sandbox-admin-test.com", confirm_id=uuid.uuid4()) == 2
    assert "no user has that email" in capsys.readouterr().out
    assert _total_admins() == before


def test_more_than_one_matching_user_is_refused():
    tag = uuid.uuid4().hex[:8]
    ids = []
    db = RealSessionLocal()
    try:
        for email in (f"Twin-{tag}@sandbox-admin-test.com", f"twin-{tag}@sandbox-admin-test.com"):  # unique index is case-sensitive
            u = User(email=email, password_hash="x")
            db.add(u)
            db.flush()
            ids.append(u.id)
        db.commit()
    finally:
        db.close()
    assert _run(email=f"twin-{tag}@sandbox-admin-test.com", confirm_id=ids[0]) == 2
    assert _admin_rows(ids[0]) == 0 and _admin_rows(ids[1]) == 0


def test_without_a_user_id_confirmation_it_only_prints_the_id_and_changes_nothing(capsys):
    _client, email, user_id = _new_user()
    assert _run(email=email) == 2
    out = capsys.readouterr().out
    assert str(user_id) in out and f"--confirm-user-id {user_id}" in out
    assert _admin_rows(user_id) == 0 and _audit_rows(user_id) == []


def test_a_wrong_or_malformed_user_id_confirmation_is_refused(capsys):
    _client, email, user_id = _new_user()
    assert _run(email=email, confirm_id=uuid.uuid4()) == 2
    assert "does not match the user found" in capsys.readouterr().out
    assert _run(email=email, confirm_id="not-a-uuid") == 2
    assert "not a valid UUID" in capsys.readouterr().out
    assert _admin_rows(user_id) == 0


# ---------------- success, duplicate, atomicity ----------------

def test_successful_grant_creates_the_admin_row_and_one_audit_row_and_actually_works(capsys):
    client, email, user_id = _new_user()
    body = {}  # deliberately invalid: an admin gets a validation error (422); a non-admin is stopped at the dependency (403)
    assert client.post("/api/v1/admin/refunds", json=body).status_code == 403

    assert _run(email=email.upper(), confirm_id=user_id) == 0  # the email is matched case-insensitively
    assert "Granted platform admin" in capsys.readouterr().out

    assert _admin_rows(user_id) == 1
    audit = _audit_rows(user_id)
    assert len(audit) == 1
    row = audit[0]
    db = RealSessionLocal()
    try:
        admin = db.query(AdminUser).filter(AdminUser.user_id == user_id).one()
        assert row.target_type == "admin_user" and row.target_id == str(admin.id)
        assert admin.granted_by is None
        assert db.get(User, user_id).is_platform_admin is False  # the unused flag is never touched
    finally:
        db.close()
    assert row.actor_user_id is None and row.organization_id is None
    assert row.audit_metadata == {"user_id": str(user_id), "via": "grant_sandbox_platform_admin", "database": _db_name(),
                                  "environment": get_settings().ENV}
    assert client.post("/api/v1/admin/refunds", json=body).status_code == 422  # same session, now an admin (no re-login needed)


def test_a_second_grant_changes_nothing_and_writes_no_second_audit_row(capsys):
    _client, email, user_id = _new_user()
    assert _run(email=email, confirm_id=user_id) == 0
    capsys.readouterr()
    assert _run(email=email, confirm_id=user_id) == 0
    assert "already a platform admin" in capsys.readouterr().out
    assert _admin_rows(user_id) == 1 and len(_audit_rows(user_id)) == 1


def test_a_failure_while_writing_the_audit_row_leaves_no_admin_behind(monkeypatch):
    _client, email, user_id = _new_user()

    def boom(*args, **kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(script, "AuditLog", boom)
    db = RealSessionLocal()
    try:
        with pytest.raises(RuntimeError):
            script.grant(db, email=email, confirm_user_id=str(user_id), database=_db_name(), environment="test")
    finally:
        db.close()
    assert _admin_rows(user_id) == 0  # the grant and its audit record are one transaction


# ---------------- integrity errors: a rollback is never reported as success ----------------

def test_a_genuine_concurrent_duplicate_grant_is_reported_as_already_admin_exactly_once():
    """Two real sessions both pass the 'is there an admin yet?' check, then both insert: the unique(user_id) index decides."""
    _client, email, user_id = _new_user()
    barrier = threading.Barrier(2, timeout=20)
    seen = threading.local()
    real_exists = script._admin_exists

    def exists_after_both_have_looked(db, uid):
        if not getattr(seen, "waited", False):
            seen.waited = True
            result = real_exists(db, uid)  # False for both: nobody is admin yet
            barrier.wait()
            return result
        return real_exists(db, uid)

    outcomes, errors = [], []

    def worker():
        db = RealSessionLocal()
        try:
            outcomes.append(script.grant(db, email=email, confirm_user_id=str(user_id), database=_db_name(), environment="test")[0])
        except Exception as exc:  # noqa: BLE001  (the test must see every failure)
            errors.append(exc)
        finally:
            db.close()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(script, "_admin_exists", exists_after_both_have_looked)
        threads = [threading.Thread(target=worker) for _ in range(2)]
        [t.start() for t in threads]
        [t.join(60) for t in threads]

    assert errors == []
    assert sorted(outcomes) == ["already_admin", "granted"]
    assert _admin_rows(user_id) == 1 and len(_audit_rows(user_id)) == 1  # one admin, one audit row (the winner's)


def test_an_integrity_error_while_inserting_the_audit_row_propagates_and_grants_nothing(monkeypatch):
    _client, email, user_id = _new_user()
    real_audit = script.AuditLog
    monkeypatch.setattr(script, "AuditLog", lambda **kw: real_audit(**{**kw, "actor_user_id": uuid.uuid4()}))  # FK to a user that does not exist
    db = RealSessionLocal()
    try:
        with pytest.raises(IntegrityError):
            script.grant(db, email=email, confirm_user_id=str(user_id), database=_db_name(), environment="test")
    finally:
        db.close()
    assert _admin_rows(user_id) == 0 and _audit_rows(user_id) == []  # rolled back together, and NOT reported as already_admin


def test_main_reports_failure_not_success_when_the_audit_insert_violates_an_integrity_constraint(monkeypatch, capsys):
    _client, email, user_id = _new_user()
    real_audit = script.AuditLog
    monkeypatch.setattr(script, "AuditLog", lambda **kw: real_audit(**{**kw, "actor_user_id": uuid.uuid4()}))
    assert _run(email=email, confirm_id=user_id) == 1
    out = capsys.readouterr().out
    assert "FAILED" in out and "NO admin was granted" in out
    assert "Granted platform admin" not in out and "already a platform admin" not in out
    assert _admin_rows(user_id) == 0


def test_an_integrity_error_on_the_admin_insert_is_not_already_admin_when_no_admin_row_exists(monkeypatch):
    _client, email, user_id = _new_user()
    real_admin = script.AdminUser
    monkeypatch.setattr(script, "_new_admin", lambda uid: real_admin(user_id=uuid.uuid4()))  # FK violation: not a duplicate of this user
    db = RealSessionLocal()
    try:
        with pytest.raises(IntegrityError):
            script.grant(db, email=email, confirm_user_id=str(user_id), database=_db_name(), environment="test")
    finally:
        db.close()
    assert _admin_rows(user_id) == 0 and _audit_rows(user_id) == []


def _verification_fails(monkeypatch, how):
    """The first _admin_exists call is the pre-check (must work); every later call - the post-commit verification - fails."""
    real = script._admin_exists
    calls = {"n": 0}

    def flaky(db, uid):
        calls["n"] += 1
        if calls["n"] == 1:
            return real(db, uid)
        if how == "raises":
            raise OperationalError("SELECT", {}, Exception("connection lost"))
        return False

    monkeypatch.setattr(script, "_admin_exists", flaky)


def test_a_verification_query_failure_after_a_successful_commit_is_not_reported_as_a_rollback(monkeypatch, capsys):
    _client, email, user_id = _new_user()
    _verification_fails(monkeypatch, "raises")
    assert _run(email=email, confirm_id=user_id) == 3
    out = capsys.readouterr().out
    assert "COMMIT SUCCEEDED, VERIFICATION FAILED" in out
    assert "NO admin was granted" not in out and "rolled back" not in out.lower().replace("not a rollback", "")
    assert "Do NOT re-run" in out and "SELECT * FROM admin_users" in out and str(user_id) in out
    assert "Granted platform admin" not in out
    assert _admin_rows(user_id) == 1 and len(_audit_rows(user_id)) == 1  # it really was committed


def test_an_admin_row_that_cannot_be_found_after_the_commit_is_a_verification_failure_not_success_or_rollback(monkeypatch, capsys):
    _client, email, user_id = _new_user()
    _verification_fails(monkeypatch, "missing")
    assert _run(email=email, confirm_id=user_id) == 3
    out = capsys.readouterr().out
    assert "COMMIT SUCCEEDED, VERIFICATION FAILED" in out and "Granted platform admin" not in out and "NO admin was granted" not in out


def test_a_failure_before_the_commit_still_says_nothing_was_granted_and_exits_1(monkeypatch, capsys):
    _client, email, user_id = _new_user()
    real_audit = script.AuditLog
    monkeypatch.setattr(script, "AuditLog", lambda **kw: real_audit(**{**kw, "actor_user_id": uuid.uuid4()}))
    assert _run(email=email, confirm_id=user_id) == 1
    out = capsys.readouterr().out
    assert "before anything was committed" in out and "NO admin was granted" in out and "COMMIT SUCCEEDED" not in out
    assert _admin_rows(user_id) == 0


def test_a_commit_that_raises_is_reported_as_unknown_outcome_never_as_rolled_back_or_granted(monkeypatch, capsys):
    _client, email, user_id = _new_user()
    real_session_factory = script.SessionLocal

    class CommitDies:
        """A real session whose commit() raises, as if the connection dropped while the server answered."""

        def __init__(self):
            self._s = real_session_factory()

        def commit(self):
            raise OperationalError("COMMIT", {}, Exception("connection lost"))

        def __getattr__(self, name):
            return getattr(self._s, name)

    monkeypatch.setattr(script, "SessionLocal", CommitDies)
    assert _run(email=email, confirm_id=user_id) == 4
    out = capsys.readouterr().out
    assert "COMMIT OUTCOME UNKNOWN" in out and "Do NOT re-run" in out and str(user_id) in out
    assert "NO admin was granted" not in out and "Granted platform admin" not in out
    assert _admin_rows(user_id) == 0  # (here the commit really never reached the server; the message must not claim that either)


class _SessionWith:
    """A real session whose chosen methods are replaced; everything else is the real thing."""

    def __init__(self, **overrides):
        self._s = RealSessionLocal()
        self._overrides = overrides

    def __getattr__(self, name):
        return self._overrides.get(name) or getattr(self._s, name)


def _failing_close(real_holder):
    def close():
        real_holder[0].close()  # still release the connection, then fail like a broken cleanup
        raise OperationalError("CLOSE", {}, Exception("connection reset during cleanup"))
    return close


def test_a_close_failure_after_a_successful_grant_is_still_reported_as_success(monkeypatch, capsys):
    _client, email, user_id = _new_user()
    holder = []

    def make():
        s = _SessionWith()
        holder.append(s._s)
        s._overrides["close"] = _failing_close(holder)
        return s

    monkeypatch.setattr(script, "SessionLocal", make)
    assert _run(email=email, confirm_id=user_id) == 0
    out = capsys.readouterr().out
    assert "Granted platform admin" in out and "closing the database session failed" in out
    assert "NO admin was granted" not in out and "FAILED" not in out
    assert _admin_rows(user_id) == 1 and len(_audit_rows(user_id)) == 1


def _close_and_logger_both_fail(monkeypatch):
    holder = []

    def make():
        s = _SessionWith()
        holder.append(s._s)
        s._overrides["close"] = _failing_close(holder)
        return s

    def broken_warning(*args, **kwargs):
        raise OSError("log handler is broken")

    monkeypatch.setattr(script, "SessionLocal", make)
    monkeypatch.setattr(script.logger, "warning", broken_warning)  # the grant's own success log uses it too: it is protected as well


def test_a_close_failure_and_a_logging_failure_together_still_report_a_successful_grant(monkeypatch, capsys):
    _client, email, user_id = _new_user()
    _close_and_logger_both_fail(monkeypatch)
    assert _run(email=email, confirm_id=user_id) == 0
    out = capsys.readouterr().out
    assert "Granted platform admin" in out and "closing the database session failed" in out
    assert "NO admin was granted" not in out and "FAILED" not in out and "UNEXPECTED" not in out
    assert _admin_rows(user_id) == 1 and len(_audit_rows(user_id)) == 1


def test_a_close_failure_and_a_logging_failure_together_keep_a_refusal_at_exit_2(monkeypatch, capsys):
    _client, email, user_id = _new_user()
    _close_and_logger_both_fail(monkeypatch)
    assert _run(email=email, confirm_id=uuid.uuid4()) == 2  # wrong user-id confirmation: refused inside grant(), then cleanup fails twice
    assert "does not match" in capsys.readouterr().out
    assert _admin_rows(user_id) == 0


def test_a_close_failure_and_a_logging_failure_together_keep_a_pre_commit_failure_at_exit_1(monkeypatch, capsys):
    _client, email, user_id = _new_user()
    real_audit = script.AuditLog
    monkeypatch.setattr(script, "AuditLog", lambda **kw: real_audit(**{**kw, "actor_user_id": uuid.uuid4()}))
    _close_and_logger_both_fail(monkeypatch)
    assert _run(email=email, confirm_id=user_id) == 1
    out = capsys.readouterr().out
    assert "before anything was committed" in out and "NO admin was granted" in out
    assert _admin_rows(user_id) == 0


def test_a_close_failure_after_already_admin_is_still_already_admin(monkeypatch, capsys):
    _client, email, user_id = _new_user()
    assert _run(email=email, confirm_id=user_id) == 0
    capsys.readouterr()
    holder = []

    def make():
        s = _SessionWith()
        holder.append(s._s)
        s._overrides["close"] = _failing_close(holder)
        return s

    monkeypatch.setattr(script, "SessionLocal", make)
    assert _run(email=email, confirm_id=user_id) == 0
    out = capsys.readouterr().out
    assert "already a platform admin" in out and "NO admin was granted" not in out
    assert _admin_rows(user_id) == 1 and len(_audit_rows(user_id)) == 1


def test_a_close_failure_never_masks_the_real_outcome_of_a_failed_run(monkeypatch, capsys):
    """Pre-commit failure + broken cleanup: still exit 1. Post-commit verification failure + broken cleanup: still exit 3."""
    # exit 1: audit row violates a foreign key before the commit
    _client, email, user_id = _new_user()
    real_audit = script.AuditLog
    monkeypatch.setattr(script, "AuditLog", lambda **kw: real_audit(**{**kw, "actor_user_id": uuid.uuid4()}))
    holder = []

    def make():
        s = _SessionWith()
        holder.append(s._s)
        s._overrides["close"] = _failing_close(holder)
        return s

    monkeypatch.setattr(script, "SessionLocal", make)
    assert _run(email=email, confirm_id=user_id) == 1
    assert "before anything was committed" in capsys.readouterr().out and _admin_rows(user_id) == 0
    monkeypatch.setattr(script, "AuditLog", real_audit)

    # exit 3: committed, verification fails, and close fails too
    _verification_fails(monkeypatch, "raises")
    assert _run(email=email, confirm_id=user_id) == 3
    out = capsys.readouterr().out
    assert "COMMIT SUCCEEDED, VERIFICATION FAILED" in out and "NO admin was granted" not in out
    assert _admin_rows(user_id) == 1


def test_an_unexpected_non_database_exception_before_the_commit_is_exit_1_and_nothing_is_committed(monkeypatch, capsys):
    _client, email, user_id = _new_user()

    def boom(**kw):
        raise ValueError("something nobody anticipated")

    monkeypatch.setattr(script, "AuditLog", boom)  # raised while staging the audit row: before the commit
    assert _run(email=email, confirm_id=user_id) == 1
    out = capsys.readouterr().out
    assert "UNEXPECTED ERROR (ValueError) before anything was committed" in out and "NO admin was granted" in out
    assert "COMMIT" not in out.replace("committed", "") and "Granted platform admin" not in out
    assert _admin_rows(user_id) == 0 and _audit_rows(user_id) == []


def test_an_unexpected_exception_while_opening_the_session_or_looking_up_the_user_is_exit_1(monkeypatch, capsys):
    _client, email, user_id = _new_user()

    def no_session():
        raise OSError("socket exploded")

    monkeypatch.setattr(script, "SessionLocal", no_session)
    assert _run(email=email, confirm_id=user_id) == 1
    assert "UNEXPECTED ERROR (OSError) before anything was committed" in capsys.readouterr().out
    monkeypatch.undo()
    monkeypatch.setattr(script, "find_exactly_one_user", lambda db, e: (_ for _ in ()).throw(KeyError("odd")))
    assert _run(email=email, confirm_id=user_id) == 1
    assert _admin_rows(user_id) == 0


def test_an_unexpected_exception_from_the_commit_is_an_unknown_outcome_never_a_known_rollback(monkeypatch, capsys):
    _client, email, user_id = _new_user()

    def make():
        return _SessionWith(commit=lambda: (_ for _ in ()).throw(ValueError("driver bug during commit")))

    monkeypatch.setattr(script, "SessionLocal", make)
    assert _run(email=email, confirm_id=user_id) == 4
    out = capsys.readouterr().out
    assert "COMMIT OUTCOME UNKNOWN" in out and "Do NOT re-run" in out and "NO admin was granted" not in out and "UNEXPECTED ERROR" not in out


def test_an_unexpected_exception_during_post_commit_verification_is_exit_3_not_exit_1(monkeypatch, capsys):
    _client, email, user_id = _new_user()
    real = script._admin_exists
    calls = {"n": 0}

    def flaky(db, uid):
        calls["n"] += 1
        if calls["n"] == 1:
            return real(db, uid)
        raise ValueError("unexpected, not a SQLAlchemy error")

    monkeypatch.setattr(script, "_admin_exists", flaky)
    assert _run(email=email, confirm_id=user_id) == 3
    out = capsys.readouterr().out
    assert "COMMIT SUCCEEDED, VERIFICATION FAILED" in out and "NO admin was granted" not in out and "UNEXPECTED ERROR" not in out
    assert _admin_rows(user_id) == 1


def test_a_refusal_still_exits_2_and_a_database_failure_exits_1(monkeypatch, capsys):
    assert script.main(["--email", "x@y.com", "--confirm-database", "wrong"]) == 2  # refused (database confirmation)
    capsys.readouterr()
    _client, email, user_id = _new_user()
    monkeypatch.setattr(script, "grant", lambda *a, **k: (_ for _ in ()).throw(IntegrityError("stmt", {}, Exception("boom"))))
    assert _run(email=email, confirm_id=user_id) == 1


# ---------------- never reachable over HTTP ----------------

def test_the_script_is_not_exposed_through_any_http_route_or_imported_by_the_api():
    paths = [r.path for r in _walk(app.routes) if isinstance(r, APIRoute)]
    assert not [p for p in paths if "grant" in p.lower() or "sandbox" in p.lower()], paths
    assert ("POST", "/api/v1/admin/refunds") in {(m, p) for r in _walk(app.routes) if isinstance(r, APIRoute) for p in [r.path] for m in r.methods}
    for source in [ROOT / "app" / "main.py", *(ROOT / "app" / "api").rglob("*.py"), *(ROOT / "app" / "services").rglob("*.py")]:
        assert "grant_sandbox_platform_admin" not in source.read_text(), f"{source} references the sandbox admin script"
    anonymous = TestClient(app)
    for url in ("/api/v1/admin/grant", "/api/v1/admin/users", "/api/v1/admin"):
        assert anonymous.post(url, json={}).status_code in (401, 404, 405)


def _walk(routes):
    for route in routes:
        nested = getattr(route, "original_router", None)
        if nested is not None:
            yield from _walk(nested.routes)
        else:
            yield route
