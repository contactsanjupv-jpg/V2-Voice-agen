"""
The test suite DROPS ALL TABLES at teardown, so conftest refuses to start unless the database is unmistakably a local, throwaway
test database. These tests pin that check: an exact name pattern (not a substring), an ENV allowlist and a local-host allowlist.
They never connect to anything: the parsing check is pure, and the end-to-end checks point at a port nothing listens on.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.db_safety import UnsafeTestDatabase, check_test_database

BACKEND = Path(__file__).resolve().parents[1]
ONE_TEST = "tests/test_production_config.py::test_paddle_env_defaults_to_sandbox_outside_production_and_accepts_only_the_two_values"


def _url(name="atla_test", host="localhost", user="atla:secret"):
    return f"postgresql+psycopg://{user}@{host}:5432/{name}"


@pytest.mark.parametrize("name", ["atla_test", "atla_ci_test", "atla2_test", "myapp_staging_test", "t_test", "atla_w8_test"])
def test_real_test_database_names_are_accepted(name):
    assert check_test_database(_url(name), "test") == name


@pytest.mark.parametrize("name", [
    "atla_latest", "atla_contest", "atla_testing", "atla_tests", "atla_test_prod", "atla_test2", "atla_attest", "latest_prod",
    "atla", "atla_dev", "atla_prod", "atla_production", "test", "_test", "atla__test", "atla-test", "Atla_Test", "atla_sandbox", "sandbox_atla", "",
])
def test_names_that_merely_contain_test_are_refused(name):
    with pytest.raises(UnsafeTestDatabase):
        check_test_database(_url(name), "test")


@pytest.mark.parametrize("env", ["production", "prod", "staging", "Production", "", "live"])
def test_only_development_and_test_environments_are_accepted(env):
    with pytest.raises(UnsafeTestDatabase):
        check_test_database(_url(), env)
    for ok in ("development", "test"):
        assert check_test_database(_url(), ok) == "atla_test"


@pytest.mark.parametrize("host", ["prod-db.example.com", "10.0.0.5", "db.internal", "LOCALHOST.evil.com", "127.0.0.1.evil.com", "atla-test.example.com"])
def test_a_remote_host_is_refused_even_with_a_test_database_name(host):
    with pytest.raises(UnsafeTestDatabase):
        check_test_database(_url(host=host), "test")


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "127.0.0.1", "[::1]"])
def test_local_hosts_are_accepted(host):
    assert check_test_database(_url(host=host), "test") == "atla_test"


def test_a_local_socket_with_no_host_is_accepted():
    assert check_test_database("postgresql+psycopg:///atla_test", "test") == "atla_test"


def test_an_unparsable_url_is_refused_and_a_password_never_appears_in_a_message():
    with pytest.raises(UnsafeTestDatabase):
        check_test_database("not a url", "test")
    with pytest.raises(UnsafeTestDatabase) as raised:
        check_test_database(_url("atla_latest", user="atla:hunter2"), "test")
    assert "hunter2" not in str(raised.value)


def _pytest_run(database_url, env="test"):
    """Start a real pytest session against a port nothing listens on: it either refuses at once or fails to connect. Never touches a database."""
    environment = {**os.environ, "DATABASE_URL": database_url, "ENV": env}
    return subprocess.run([sys.executable, "-m", "pytest", ONE_TEST, "-q", "-p", "no:cacheprovider"],
                          cwd=BACKEND, env=environment, capture_output=True, text=True, timeout=120)


def test_conftest_refuses_to_start_the_suite_against_atla_latest():
    result = _pytest_run("postgresql+psycopg://atla:x@127.0.0.1:1/atla_latest")
    assert result.returncode == 1
    assert "REFUSING TO RUN" in result.stdout + result.stderr
    assert "atla_latest" in result.stdout + result.stderr
    assert "Connection refused" not in result.stdout + result.stderr  # refused before any connection was attempted


def test_conftest_refuses_a_remote_host_and_a_production_env():
    remote = _pytest_run("postgresql+psycopg://atla:x@prod-db.example.com:1/atla_test")
    assert remote.returncode == 1 and "REFUSING TO RUN" in remote.stdout + remote.stderr
    assert "Connection refused" not in remote.stdout + remote.stderr and "could not translate host" not in remote.stdout + remote.stderr
    production = _pytest_run("postgresql+psycopg://atla:x@127.0.0.1:1/atla_test", env="production")
    out = production.stdout + production.stderr
    assert production.returncode != 0 and "Connection refused" not in out  # never reached a connection, whatever stopped it first


def test_conftest_lets_a_proper_test_database_through_to_the_connection_step():
    result = _pytest_run("postgresql+psycopg://atla:x@127.0.0.1:1/atla_test")
    output = result.stdout + result.stderr
    assert "Connection refused" in output  # the guard accepted it and the suite went on to connect; nothing listens on port 1
    assert result.returncode != 0
