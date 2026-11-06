"""
Strict safety check for the database the test suite runs against. The suite DROPS EVERY TABLE at teardown, so it must start only
against a database that is unmistakably a disposable, local test database. Pure parsing: no connection is opened.

Replaces the old `"test" in db_name.lower()` check, which also accepted atla_latest, atla_contest and atla_testing.
"""
import re

from app.db.url_safety import UnsafeDatabaseUrl, check_local_database_url

# Explicit allowlists, not "anything that is not production".
SAFE_ENVIRONMENTS = frozenset({"development", "test"})  # ENV defaults to "development", so a bare local run is allowed
TEST_DATABASE_NAME = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*_test$")  # the LAST '_'-word is exactly "test": atla_test, atla_ci_test


class UnsafeTestDatabase(Exception):
    """The configured database is not one the test suite may drop. The message never contains a password."""


def check_test_database(database_url: str, env: str) -> str:
    """Returns the database name, or raises UnsafeTestDatabase."""
    if env not in SAFE_ENVIRONMENTS:
        raise UnsafeTestDatabase(f"ENV={env!r} is not one of {sorted(SAFE_ENVIRONMENTS)}.")
    try:  # the SAME fail-closed routing check the admin-grant script uses (app/db/url_safety.py)
        url = check_local_database_url(database_url)
    except UnsafeDatabaseUrl as unsafe:
        raise UnsafeTestDatabase(str(unsafe)) from None
    name = url.database or ""
    if not TEST_DATABASE_NAME.fullmatch(name):
        raise UnsafeTestDatabase(
            f"the database name '{name}' is not a recognised test database name "
            "(lower-case words joined by '_' whose LAST word is exactly 'test', e.g. atla_test)."
        )
    return name
