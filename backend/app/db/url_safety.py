"""
ONE definition of "this DATABASE_URL can only reach a database on THIS machine", shared by the sandbox admin-grant script and the
destructive test-suite guard so the two can never disagree. Pure parsing: nothing here opens a connection.

Looking at `url.host` alone is not enough. libpq lets the query string, and the process environment, re-route the connection:
?host=prod.example.com, ?hostaddr=..., repeated or comma-separated hosts, ?service=..., PGHOST / PGHOSTADDR / PGSERVICE, and socket
directories. So this check FAILS CLOSED: it accepts a URL only if every routing-relevant part is known and provably local, and it
refuses anything it does not recognise.

WHAT THIS CHECK CANNOT PROVE. It validates CONFIGURATION, not the network. A URL port, and the PGPORT environment variable, are NOT
validated for forwarding: "localhost:5433" is accepted even if something on this machine tunnels that port to a remote database (ssh -L,
a proxy, a container port mapping). The name "localhost" is trusted as written, so a hosts file or DNS entry that maps it elsewhere would
also defeat the check. A local host and port therefore do NOT prove the destination is local, and a database name ending in "_test" does
not prove the database is disposable.
"""
import os
import posixpath
from collections.abc import Mapping
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

# Exactly the driver this application is configured with (psycopg 3: config.py, .env.example, CI, README, requirements.txt) and the only one
# whose connection arguments and environment handling were reviewed. Exact match: plain "postgresql" (SQLAlchemy's alias for psycopg2),
# postgresql+psycopg2, asyncpg, pg8000, ... are refused; the application could not use them here anyway.
ALLOWED_DRIVERS = frozenset({"postgresql+psycopg"})
SAFE_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
SAFE_HOSTADDRS = frozenset({"127.0.0.1", "::1"})
# A unix socket can only be on this machine, but a socket path is still caller-controlled text: only these standard directories are
# proven safe. Any other path is refused rather than reasoned about.
SAFE_SOCKET_DIRECTORIES = frozenset({"/var/run/postgresql", "/run/postgresql", "/tmp", "/var/tmp"})
# The ONLY query parameters accepted. Everything else (service, passfile, target_session_attrs, load_balance_hosts, port, dbname, user,
# options, ...) is refused: a parameter this check does not understand could be routing the connection somewhere else.
ALLOWED_QUERY_PARAMETERS = frozenset({"host", "hostaddr", "sslmode", "connect_timeout", "application_name", "client_encoding"})


class UnsafeDatabaseUrl(Exception):
    """The URL could reach a non-local database, or is ambiguous. The message never contains a password."""


def _single(value) -> str:
    """A query value must be ONE string. A repeated parameter (host=a&host=b) arrives as a tuple: ambiguous, so refused."""
    if isinstance(value, (tuple, list)):
        raise UnsafeDatabaseUrl("a connection parameter is repeated, so where it connects is ambiguous.")
    return value


def _check_host_value(value: str, *, where: str) -> None:
    if not value:
        raise UnsafeDatabaseUrl(f"the database host in {where} is empty, so where it connects is ambiguous.")
    if "," in value:
        raise UnsafeDatabaseUrl(f"the database host '{value}' in {where} lists several hosts, which is not local-only.")
    if value.startswith("/"):
        if value != posixpath.normpath(value) or value not in SAFE_SOCKET_DIRECTORIES:  # exact match: no "..", "//", trailing "/" tricks
            raise UnsafeDatabaseUrl(
                f"the socket directory '{value}' in {where} is not on the safe list {sorted(SAFE_SOCKET_DIRECTORIES)}."
            )
        return
    if value.lower() not in SAFE_HOSTS:
        raise UnsafeDatabaseUrl(f"the database host '{value}' in {where} is not local (allowed: {sorted(SAFE_HOSTS)} or a local socket).")


def check_local_database_url(database_url: str, environ: Mapping[str, str] | None = None) -> URL:
    """Returns the parsed URL, or raises UnsafeDatabaseUrl. `environ` defaults to the real process environment."""
    environ = os.environ if environ is None else environ
    try:
        url = make_url(database_url)
    except (ArgumentError, ValueError):  # a malformed port ("host:", "host:5432,5433", "host:5432:6000") raises ValueError, not ArgumentError
        raise UnsafeDatabaseUrl("DATABASE_URL could not be parsed.") from None
    if url.drivername not in ALLOWED_DRIVERS:
        raise UnsafeDatabaseUrl(f"the database driver '{url.drivername}' is not supported here (allowed: {sorted(ALLOWED_DRIVERS)}).")

    # SQLAlchemy silently drops blank query values (?host=), but a different parser may not: an empty parameter is ambiguous, so refuse it.
    if any(value == "" for _key, value in parse_qsl(urlsplit(database_url).query, keep_blank_values=True)):
        raise UnsafeDatabaseUrl("a connection parameter in the URL is empty, so where it connects is ambiguous.")
    unknown = sorted(set(url.query) - ALLOWED_QUERY_PARAMETERS)
    if unknown:
        raise UnsafeDatabaseUrl(f"the connection parameter(s) {unknown} are not supported here: they could route the connection elsewhere.")
    query = {key: _single(value) for key, value in url.query.items()}

    if url.host and "host" in query:
        raise UnsafeDatabaseUrl("the host is given twice (in the URL and as ?host=), so where it connects is ambiguous.")
    if url.host:
        _check_host_value(url.host, where="the URL")
    elif "host" in query:
        _check_host_value(query["host"], where="?host=")
    elif environ.get("PGHOST"):  # no host anywhere in the URL: libpq would read PGHOST
        _check_host_value(environ["PGHOST"], where="the PGHOST environment variable")
    # else: no host at all = libpq's default local unix socket

    if "hostaddr" in query and query["hostaddr"] not in SAFE_HOSTADDRS:
        raise UnsafeDatabaseUrl(f"hostaddr '{query['hostaddr']}' is not a local address (allowed: {sorted(SAFE_HOSTADDRS)}).")
    if environ.get("PGHOSTADDR") and environ["PGHOSTADDR"] not in SAFE_HOSTADDRS:
        raise UnsafeDatabaseUrl("the PGHOSTADDR environment variable points at a non-local address.")
    for variable in ("PGSERVICE", "PGSERVICEFILE"):
        if environ.get(variable):
            raise UnsafeDatabaseUrl(f"the {variable} environment variable can supply the host, which this check cannot see.")
    return url
