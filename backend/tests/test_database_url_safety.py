"""
`url.host` alone does not say where a PostgreSQL connection goes: libpq also honours ?host=, ?hostaddr=, repeated and comma-separated
hosts, socket directories, ?service= and the PGHOST / PGHOSTADDR / PGSERVICE environment variables. The shared check
(app/db/url_safety.py) fails closed on all of them, and BOTH destructive-or-privileged guards use it: the sandbox admin-grant script and
the test-suite guard in conftest. Every bypass below must be refused before any connection, name resolution or session is attempted.
"""
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import get_settings
from app.db.url_safety import UnsafeDatabaseUrl, check_local_database_url
from app.workers import grant_sandbox_platform_admin as script
from tests.db_safety import UnsafeTestDatabase, check_test_database

BACKEND = Path(__file__).resolve().parents[1]
ONE_TEST = "tests/test_production_config.py::test_paddle_env_defaults_to_sandbox_outside_production_and_accepts_only_the_two_values"
BASE = "postgresql+psycopg://atla:hunter2@{host}/atla_test"

# (label, url, environment). Each one routes - or could route - the connection to a machine that is not this one.
BYPASSES = [
    ("?host= remote, no URL host", "postgresql+psycopg://atla:hunter2@/atla_test?host=prod-db.example.com", {}),
    ("?host= remote beside a local URL host", "postgresql+psycopg://atla:hunter2@localhost/atla_test?host=prod-db.example.com", {}),
    ("?host= given twice", "postgresql+psycopg://atla:hunter2@/atla_test?host=localhost&host=prod-db.example.com", {}),
    ("?host= repeated, both local", "postgresql+psycopg://atla:hunter2@/atla_test?host=localhost&host=127.0.0.1", {}),
    ("?host= comma list", "postgresql+psycopg://atla:hunter2@/atla_test?host=localhost,prod-db.example.com", {}),
    ("URL host comma list", "postgresql+psycopg://atla:hunter2@localhost,prod-db.example.com:5432/atla_test", {}),
    ("?host= empty", "postgresql+psycopg://atla:hunter2@/atla_test?host=", {}),
    ("?host= different case of key", "postgresql+psycopg://atla:hunter2@/atla_test?Host=prod-db.example.com", {}),
    ("?hostaddr= remote with a local host name", "postgresql+psycopg://atla:hunter2@localhost/atla_test?hostaddr=203.0.113.9", {}),
    ("?hostaddr= remote, no host", "postgresql+psycopg://atla:hunter2@/atla_test?hostaddr=10.0.0.5", {}),
    ("?hostaddr= repeated", "postgresql+psycopg://atla:hunter2@localhost/atla_test?hostaddr=127.0.0.1&hostaddr=10.0.0.5", {}),
    ("socket dir outside the safe list", "postgresql+psycopg://atla:hunter2@/atla_test?host=/mnt/shared/sockets", {}),
    ("socket dir with ..", "postgresql+psycopg://atla:hunter2@/atla_test?host=/var/run/postgresql/../../mnt/x", {}),
    ("socket dir with trailing slash", "postgresql+psycopg://atla:hunter2@/atla_test?host=/tmp/", {}),
    ("socket dir with doubled slash", "postgresql+psycopg://atla:hunter2@/atla_test?host=//tmp", {}),
    ("macOS /private/tmp is not on the safe list", "postgresql+psycopg://atla:hunter2@/atla_test?host=/private/tmp", {}),
    ("relative socket path", "postgresql+psycopg://atla:hunter2@/atla_test?host=sockets", {}),
    ("socket dir listed with a remote host", "postgresql+psycopg://atla:hunter2@/atla_test?host=/tmp,prod-db.example.com", {}),
    ("?service=", "postgresql+psycopg://atla:hunter2@localhost/atla_test?service=prod", {}),
    ("?servicefile=", "postgresql+psycopg://atla:hunter2@localhost/atla_test?servicefile=/tmp/pg_service.conf", {}),
    ("?passfile=", "postgresql+psycopg://atla:hunter2@localhost/atla_test?passfile=/tmp/x", {}),
    ("?port= override", "postgresql+psycopg://atla:hunter2@localhost/atla_test?port=5432,5433", {}),
    ("?dbname= override", "postgresql+psycopg://atla:hunter2@localhost/atla_test?dbname=atla_prod", {}),
    ("?user= override", "postgresql+psycopg://atla:hunter2@localhost/atla_test?user=postgres", {}),
    ("?target_session_attrs=", "postgresql+psycopg://atla:hunter2@localhost/atla_test?target_session_attrs=any", {}),
    ("?load_balance_hosts=", "postgresql+psycopg://atla:hunter2@localhost/atla_test?load_balance_hosts=random", {}),
    ("?options=", "postgresql+psycopg://atla:hunter2@localhost/atla_test?options=-csearch_path%3Dx", {}),
    ("unknown parameter", "postgresql+psycopg://atla:hunter2@localhost/atla_test?something_new=1", {}),
    ("non-PostgreSQL driver", "mysql+pymysql://atla:hunter2@localhost/atla_test", {}),
    ("sqlite driver", "sqlite:///atla_test", {}),
    ("plain postgresql driver (psycopg2 alias)", "postgresql://atla:hunter2@localhost/atla_test", {}),
    ("postgresql+psycopg2 driver", "postgresql+psycopg2://atla:hunter2@localhost/atla_test", {}),
    ("malformed port: empty", "postgresql+psycopg://atla:hunter2@localhost:/atla_test", {}),
    ("malformed port: comma list", "postgresql+psycopg://atla:hunter2@localhost:5432,5433/atla_test", {}),
    ("malformed port: two ports", "postgresql+psycopg://atla:hunter2@localhost:5432:6000/atla_test", {}),
    ("malformed port: not a number", "postgresql+psycopg://atla:hunter2@localhost:abc/atla_test", {}),
    ("postgresql+asyncpg", "postgresql+asyncpg://atla:hunter2@localhost/atla_test", {}),
    ("postgresql+pg8000", "postgresql+pg8000://atla:hunter2@localhost/atla_test", {}),
    ("postgresql+psycopg2cffi", "postgresql+psycopg2cffi://atla:hunter2@localhost/atla_test", {}),
    ("postgresql+pypostgresql", "postgresql+pypostgresql://atla:hunter2@localhost/atla_test", {}),
    ("postgres alias", "postgres://atla:hunter2@localhost/atla_test", {}),
    ("driver that only starts with postgresql", "postgresqlx://atla:hunter2@localhost/atla_test", {}),
    ("empty driver suffix", "postgresql+://atla:hunter2@localhost/atla_test", {}),
    ("extra driver suffix", "postgresql+psycopg+extra://atla:hunter2@localhost/atla_test", {}),
    ("driver in a different case", "PostgreSQL+psycopg://atla:hunter2@localhost/atla_test", {}),
    ("PGHOST remote, URL names no host", "postgresql+psycopg://atla:hunter2@/atla_test", {"PGHOST": "prod-db.example.com"}),
    ("PGHOST comma list, URL names no host", "postgresql+psycopg://atla:hunter2@/atla_test", {"PGHOST": "localhost,prod-db.example.com"}),
    ("PGHOSTADDR remote beside a local host", "postgresql+psycopg://atla:hunter2@localhost/atla_test", {"PGHOSTADDR": "203.0.113.9"}),
    ("PGSERVICE set", "postgresql+psycopg://atla:hunter2@localhost/atla_test", {"PGSERVICE": "prod"}),
    ("PGSERVICEFILE set", "postgresql+psycopg://atla:hunter2@localhost/atla_test", {"PGSERVICEFILE": "/tmp/pg_service.conf"}),
]
LABELS = [b[0] for b in BYPASSES]

# Legitimate local configurations that must keep working.
LOCAL = [
    ("localhost", BASE.format(host="localhost:5432"), {}),
    ("LOCALHOST", BASE.format(host="LOCALHOST:5432"), {}),
    ("127.0.0.1", BASE.format(host="127.0.0.1:5432"), {}),
    ("::1", BASE.format(host="[::1]:5432"), {}),
    ("default local socket (no host)", "postgresql+psycopg://atla:hunter2@/atla_test", {}),
    ("?host= standard socket dir /var/run/postgresql", "postgresql+psycopg://atla:hunter2@/atla_test?host=/var/run/postgresql", {}),
    ("?host= /run/postgresql", "postgresql+psycopg://atla:hunter2@/atla_test?host=/run/postgresql", {}),
    ("?host= /tmp", "postgresql+psycopg://atla:hunter2@/atla_test?host=/tmp", {}),
    ("?host= localhost, no URL host", "postgresql+psycopg://atla:hunter2@/atla_test?host=localhost", {}),
    ("?hostaddr= a loopback address", "postgresql+psycopg://atla:hunter2@localhost/atla_test?hostaddr=127.0.0.1", {}),
    ("harmless parameters", BASE.format(host="localhost:5432") + "?sslmode=disable&connect_timeout=5&application_name=atla", {}),
    ("PGHOST local, no URL host", "postgresql+psycopg://atla:hunter2@/atla_test", {"PGHOST": "/var/run/postgresql"}),
    ("PGHOST remote but the URL names a local host (the URL wins)", BASE.format(host="localhost:5432"), {"PGHOST": "prod-db.example.com"}),
    ("PGHOSTADDR loopback", BASE.format(host="localhost:5432"), {"PGHOSTADDR": "127.0.0.1"}),
    ("PGREQUIRESSL is a security setting, not a destination", BASE.format(host="localhost:5432"), {"PGREQUIRESSL": "1"}),
]


@pytest.fixture
def no_connection(monkeypatch):
    """Any connection attempt, DNS lookup or database session in these tests is a failure: the refusal has to come first."""
    def trap(*args, **kwargs):
        raise AssertionError("the guard tried to connect, resolve a name or open a session before refusing")

    import psycopg

    monkeypatch.setattr(psycopg, "connect", trap)
    monkeypatch.setattr(socket.socket, "connect", trap)
    monkeypatch.setattr(socket, "getaddrinfo", trap)
    monkeypatch.setattr(script, "SessionLocal", trap)


def _env(monkeypatch, environment):
    for variable in ("PGHOST", "PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"):
        monkeypatch.delenv(variable, raising=False)
    for variable, value in environment.items():
        monkeypatch.setenv(variable, value)


# ---------------- the shared check ----------------

@pytest.mark.parametrize("label,url,environment", BYPASSES, ids=LABELS)
def test_shared_check_refuses_every_routing_bypass(no_connection, label, url, environment):
    with pytest.raises(UnsafeDatabaseUrl) as refused:
        check_local_database_url(url, environment)
    assert "hunter2" not in str(refused.value)  # a password never appears in a refusal


@pytest.mark.parametrize("label,url,environment", LOCAL, ids=[c[0] for c in LOCAL])
def test_shared_check_still_accepts_genuine_local_configurations(no_connection, label, url, environment):
    assert check_local_database_url(url, environment).database == "atla_test"


@pytest.mark.parametrize("url", [
    "postgresql+psycopg://atla:x@/atla_test?host=localhost,127.0.0.1",
    "postgresql+psycopg://atla:x@localhost,127.0.0.1:5432/atla_test",
    "postgresql+psycopg://atla:x@/atla_test?host=/tmp,/var/tmp",
])
def test_a_host_list_is_refused_as_such_even_when_every_entry_is_local(url):
    with pytest.raises(UnsafeDatabaseUrl, match="lists several hosts"):
        check_local_database_url(url, {})


def test_shared_check_reads_the_real_environment_by_default(monkeypatch):
    _env(monkeypatch, {"PGHOST": "prod-db.example.com"})
    with pytest.raises(UnsafeDatabaseUrl):
        check_local_database_url("postgresql+psycopg://atla:x@/atla_test")
    _env(monkeypatch, {})
    assert check_local_database_url("postgresql+psycopg://atla:x@/atla_test").database == "atla_test"


# ---------------- the test-suite guard uses it ----------------

@pytest.mark.parametrize("label,url,environment", BYPASSES, ids=LABELS)
def test_the_test_suite_guard_refuses_every_bypass(monkeypatch, no_connection, label, url, environment):
    _env(monkeypatch, environment)
    with pytest.raises(UnsafeTestDatabase) as refused:
        check_test_database(url, "test")
    assert "hunter2" not in str(refused.value)


@pytest.mark.parametrize("label,url,environment", LOCAL, ids=[c[0] for c in LOCAL])
def test_the_test_suite_guard_still_accepts_genuine_local_configurations(monkeypatch, no_connection, label, url, environment):
    _env(monkeypatch, environment)
    assert check_test_database(url, "test") == "atla_test"


# ---------------- the admin-grant script uses it ----------------

def _settings(url):
    return get_settings().model_copy(update={"DATABASE_URL": url, "ENV": "test"})


@pytest.mark.parametrize("label,url,environment", BYPASSES, ids=LABELS)
def test_the_admin_grant_script_refuses_every_bypass_before_any_session(monkeypatch, no_connection, capsys, label, url, environment):
    _env(monkeypatch, environment)
    with pytest.raises(script.Refused) as refused:
        script.check_environment(_settings(url), "atla_test")
    assert "hunter2" not in str(refused.value)
    # and through main(): exit 2, nothing printed that leaks the password, no session opened (the trap would fail the test)
    monkeypatch.setattr(get_settings(), "DATABASE_URL", url)
    assert script.main(["--email", "x@example.com", "--confirm-database", "atla_test", "--confirm-user-id", "1"]) == 2
    assert "hunter2" not in capsys.readouterr().out


@pytest.mark.parametrize("label,url,environment", LOCAL, ids=[c[0] for c in LOCAL])
def test_the_admin_grant_script_still_accepts_genuine_local_configurations(monkeypatch, no_connection, label, url, environment):
    _env(monkeypatch, environment)
    assert script.check_environment(_settings(url), "atla_test") == "atla_test"


def test_both_guards_call_the_same_function_so_they_cannot_disagree():
    import tests.db_safety as suite_guard

    assert suite_guard.check_local_database_url is check_local_database_url is script.check_local_database_url


# ---------------- conftest refuses a bypass before connecting ----------------

def _pytest_run(url, environment=None):
    env = {**os.environ, "DATABASE_URL": url, "ENV": "test", **(environment or {})}
    return subprocess.run([sys.executable, "-m", "pytest", ONE_TEST, "-q", "-p", "no:cacheprovider"],
                          cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("url", [
    "postgresql+psycopg://atla:x@/atla_test?host=prod-db.example.com",
    "postgresql+psycopg://atla:x@localhost:1/atla_test?hostaddr=203.0.113.9",
    "postgresql+psycopg://atla:x@/atla_test?host=localhost&host=prod-db.example.com",
    "postgresql+psycopg://atla:x@/atla_test?host=/mnt/shared/sockets",
], ids=["?host=", "?hostaddr=", "repeated host", "socket dir"])
def test_conftest_refuses_a_routing_bypass_before_connecting_or_resolving_anything(url):
    result = _pytest_run(url)
    out = result.stdout + result.stderr
    assert result.returncode == 1 and "REFUSING TO RUN" in out
    assert "Connection refused" not in out and "could not translate host" not in out and "OperationalError" not in out


# ---------------- the allowlist, the documentation and libpq's own list of environment variables ----------------

def test_the_socket_directory_allowlist_is_exactly_what_the_documentation_says():
    from app.db.url_safety import SAFE_SOCKET_DIRECTORIES

    assert SAFE_SOCKET_DIRECTORIES == {"/var/run/postgresql", "/run/postgresql", "/tmp", "/var/tmp"}
    documentation = (BACKEND.parent / "docs" / "SANDBOX_PLATFORM_ADMIN.md").read_text()
    for directory in SAFE_SOCKET_DIRECTORIES:
        assert f"`{directory}`" in documentation, f"{directory} is allowed in code but not documented"


# Every environment variable libpq maps to a connection parameter, and why it cannot send a connection somewhere unchecked.
CHECKED = {"PGHOST", "PGHOSTADDR", "PGSERVICE"}  # read by check_local_database_url
REQUIRED_IN_URL = {"PGDATABASE"}  # a database name must be IN the URL and match the test/sandbox pattern, so the variable is never consulted
SINGLE_HOST_ONLY = {"PGTARGETSESSIONATTRS", "PGLOADBALANCEHOSTS"}  # only choose between several hosts; host lists are refused
PORT_ONLY = {"PGPORT"}  # same machine, different port: the same limitation as a port written in the URL
NOT_A_DESTINATION = {  # credentials, TLS, authentication, encoding, timeouts, session options: none chooses a server
    "PGUSER", "PGPASSWORD", "PGPASSFILE", "PGCHANNELBINDING", "PGCONNECT_TIMEOUT", "PGCLIENTENCODING", "PGOPTIONS", "PGAPPNAME",
    "PGSSLMODE", "PGSSLNEGOTIATION", "PGSSLCOMPRESSION", "PGSSLCERT", "PGSSLKEY", "PGSSLCERTMODE", "PGSSLROOTCERT", "PGSSLCRL",
    "PGSSLCRLDIR", "PGSSLSNI", "PGREQUIREPEER", "PGREQUIREAUTH", "PGSSLMINPROTOCOLVERSION", "PGSSLMAXPROTOCOLVERSION", "PGGSSENCMODE",
    "PGKRBSRVNAME", "PGGSSLIB", "PGGSSDELEGATION",
    # Deprecated in PostgreSQL 17 in favour of PGSSLMODE ("behaves the same as the requiressl connection parameter"). A TLS requirement only,
    # never a host. The installed libpq does not list it among its connection options, so it is classified here explicitly.
    "PGREQUIRESSL",
}


def test_every_libpq_environment_variable_that_feeds_a_connection_parameter_has_been_considered():
    """Asks the installed libpq (no connection) which environment variables it reads. A new one, e.g. after a libpq upgrade, fails here
    until someone decides whether it can change where the connection goes."""
    from psycopg import pq

    libpq_variables = {option.envvar.decode() for option in pq.Conninfo.get_defaults() if option.envvar}
    considered = CHECKED | REQUIRED_IN_URL | SINGLE_HOST_ONLY | PORT_ONLY | NOT_A_DESTINATION
    assert libpq_variables <= considered, f"unreviewed libpq environment variables: {sorted(libpq_variables - considered)}"
    assert CHECKED <= libpq_variables  # and the ones we check really are libpq's


@pytest.mark.parametrize("variable", sorted(CHECKED))
def test_each_checked_environment_variable_is_actually_enforced(variable):
    value = {"PGHOST": "prod-db.example.com", "PGHOSTADDR": "203.0.113.9", "PGSERVICE": "prod"}[variable]
    url = "postgresql+psycopg://atla:x@/atla_test" if variable == "PGHOST" else "postgresql+psycopg://atla:x@localhost/atla_test"
    with pytest.raises(UnsafeDatabaseUrl):
        check_local_database_url(url, {variable: value})


def test_a_host_list_in_pghostaddr_is_refused_even_if_it_starts_with_a_loopback_address():
    with pytest.raises(UnsafeDatabaseUrl):
        check_local_database_url("postgresql+psycopg://atla:x@localhost/atla_test", {"PGHOSTADDR": "127.0.0.1,203.0.113.9"})


def test_pgrequiressl_is_explicitly_classified_as_a_security_setting_and_never_changes_the_verdict():
    assert "PGREQUIRESSL" in NOT_A_DESTINATION and not ({"PGREQUIRESSL"} & (CHECKED | REQUIRED_IN_URL | SINGLE_HOST_ONLY | PORT_ONLY))
    local = "postgresql+psycopg://atla:x@localhost/atla_test"
    assert check_local_database_url(local, {"PGREQUIRESSL": "1"}).database == "atla_test"
    assert check_local_database_url(local, {"PGREQUIRESSL": ""}).database == "atla_test"
    with pytest.raises(UnsafeDatabaseUrl):  # and it cannot launder a remote host
        check_local_database_url("postgresql+psycopg://atla:x@prod-db.example.com/atla_test", {"PGREQUIRESSL": "1"})


def test_the_environment_variable_classes_do_not_overlap():
    classes = [CHECKED, REQUIRED_IN_URL, SINGLE_HOST_ONLY, PORT_ONLY, NOT_A_DESTINATION]
    assert sum(len(c) for c in classes) == len(set().union(*classes)), "a variable is classified twice"


def test_only_the_applications_own_driver_is_allowed():
    from app.db.url_safety import ALLOWED_DRIVERS

    assert ALLOWED_DRIVERS == {"postgresql+psycopg"}
    assert check_local_database_url("postgresql+psycopg://atla:x@localhost/atla_test", {}).database == "atla_test"


def test_the_allowed_driver_is_the_one_the_application_is_configured_with():
    """Ties the allowlist to the real configuration so the two cannot drift apart again."""
    from app.db.url_safety import ALLOWED_DRIVERS

    repo = BACKEND.parent
    for path in (BACKEND / ".env.example", repo / ".github" / "workflows" / "ci.yml", BACKEND / "README.md"):
        text = path.read_text()
        assert "postgresql+psycopg://" in text, f"{path.name} does not use the allowed driver"
        assert "postgresql+psycopg2" not in text and "postgresql+asyncpg" not in text
    assert "psycopg[binary]" in (BACKEND / "requirements.txt").read_text()
    assert ALLOWED_DRIVERS == {"postgresql+psycopg"}


@pytest.mark.parametrize("driver", [
    "postgresql", "postgresql+psycopg2", "postgresql+asyncpg", "postgresql+pg8000", "postgresql+psycopg2cffi", "postgresql+pypostgresql", "postgres", "postgresqlx",
    "postgresql+", "postgresql+psycopg+extra", "PostgreSQL+psycopg", "POSTGRESQL", "mysql", "mysql+pymysql", "sqlite", "oracle+oracledb",
])
def test_every_other_driver_is_refused_with_a_message_naming_the_driver(driver):
    with pytest.raises(UnsafeDatabaseUrl, match="driver"):
        check_local_database_url(f"{driver}://atla:hunter2@localhost/atla_test", {})


# ---------------- a malformed port is a deliberate refusal, never an unexpected error ----------------

MALFORMED_PORTS = [
    "postgresql+psycopg://atla:hunter2@localhost:/atla_test",
    "postgresql+psycopg://atla:hunter2@localhost:5432,5433/atla_test",
    "postgresql+psycopg://atla:hunter2@localhost:5432:6000/atla_test",
]


@pytest.mark.parametrize("url", MALFORMED_PORTS)
def test_a_malformed_port_is_refused_with_the_safety_exception_not_a_valueerror(no_connection, monkeypatch, url):
    with pytest.raises(UnsafeDatabaseUrl, match="could not be parsed") as shared:
        check_local_database_url(url, {})
    assert not isinstance(shared.value, ValueError)
    _env(monkeypatch, {})
    with pytest.raises(UnsafeTestDatabase, match="could not be parsed"):  # the suite guard: its own refusal type
        check_test_database(url, "test")
    with pytest.raises(script.Refused, match="could not be parsed"):  # the script: a refusal (exit 2), not "UNEXPECTED ERROR" (exit 1)
        script.check_environment(_settings(url), "atla_test")


@pytest.mark.parametrize("url", MALFORMED_PORTS)
def test_the_admin_grant_script_exits_2_not_1_for_a_malformed_port(no_connection, monkeypatch, capsys, url):
    monkeypatch.setattr(get_settings(), "DATABASE_URL", url)
    assert script.main(["--email", "x@example.com", "--confirm-database", "atla_test", "--confirm-user-id", "1"]) == 2
    out = capsys.readouterr().out
    assert "could not be parsed" in out and "UNEXPECTED ERROR" not in out and "hunter2" not in out


def test_url_ports_and_pgport_are_documented_as_unchecked():
    """The limitation is stated where a reader will look for it, in the module and in the operator documentation."""
    import app.db.url_safety as module

    documentation = (BACKEND.parent / "docs" / "SANDBOX_PLATFORM_ADMIN.md").read_text()
    for text, no_proof in ((module.__doc__, "do NOT prove"), (documentation, "do **not** prove")):
        assert "PGPORT" in text and "tunnel" in text and no_proof in text
    assert not hasattr(module, "ROUTING_ENVIRONMENT")
    # and a forwarded-looking local port is, as documented, accepted: the check cannot see forwarding
    assert check_local_database_url("postgresql+psycopg://atla:x@localhost:6543/atla_test", {"PGPORT": "6543"}).port == 6543
