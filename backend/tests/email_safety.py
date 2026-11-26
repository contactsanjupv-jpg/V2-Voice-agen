"""
The test suite must NEVER reach a real mail server, whatever .env contains.

Layer 1 (app/core/email.py): in test mode `send_email` opens no connection at all. That is the layer that matters.
Layer 2 (here): if any other code path opens an SMTP connection to a non-local host (a new module using smtplib directly, a
swallowed exception, a different library on a mail port), the attempt is refused before any I/O AND recorded, and the test that
made it FAILS at teardown even if the code swallowed the exception.
Layer 3 (conftest): the real SMTP credentials are scrubbed from the settings object, so nothing can read them.

Pure standard library; nothing here opens a connection.
"""
import ipaddress
import smtplib
import socket

SMTP_PORTS = frozenset({25, 465, 587, 2465, 2525, 2587})
violations: list[str] = []


class RealSMTPBlocked(AssertionError):
    """A test (or the code it ran) tried to open a connection to a real mail server."""


def is_loopback(host) -> bool:
    text = str(host or "").strip().lower()
    if text == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _refuse(kind: str, host, port) -> None:
    message = f"{kind} to {host}:{port}"
    violations.append(message)
    raise RealSMTPBlocked(f"BLOCKED: a test tried to open a real mail connection ({message}). Tests must never send email; use the `outbox` fixture.")


def take_violations() -> list[str]:
    """For the tests of this guard itself: returns and clears what was recorded, so the autouse check does not fail them."""
    taken = list(violations)
    violations.clear()
    return taken


def install():
    """Installs the guards; returns a function that removes them."""
    originals = {
        "smtp_init": smtplib.SMTP.__init__,
        "smtp_ssl_init": smtplib.SMTP_SSL.__init__,
        "create_connection": socket.create_connection,
        "connect": socket.socket.connect,
        "connect_ex": socket.socket.connect_ex,
    }

    def smtp_init(self, host="", port=0, *args, **kwargs):
        if host and not is_loopback(host):  # smtplib connects inside __init__ whenever a host is given
            _refuse("smtplib.SMTP", host, port)
        return originals["smtp_init"](self, host, port, *args, **kwargs)

    def smtp_ssl_init(self, host="", port=0, *args, **kwargs):
        if host and not is_loopback(host):
            _refuse("smtplib.SMTP_SSL", host, port)
        return originals["smtp_ssl_init"](self, host, port, *args, **kwargs)

    def create_connection(address, *args, **kwargs):
        host, port = address[0], address[1]
        if int(port) in SMTP_PORTS and not is_loopback(host):
            _refuse("socket.create_connection", host, port)
        return originals["create_connection"](address, *args, **kwargs)

    def connect(self, address):
        if isinstance(address, tuple) and len(address) >= 2 and int(address[1]) in SMTP_PORTS and not is_loopback(address[0]):
            _refuse("socket.connect", address[0], address[1])
        return originals["connect"](self, address)

    def connect_ex(self, address):
        if isinstance(address, tuple) and len(address) >= 2 and int(address[1]) in SMTP_PORTS and not is_loopback(address[0]):
            _refuse("socket.connect_ex", address[0], address[1])
        return originals["connect_ex"](self, address)

    smtplib.SMTP.__init__ = smtp_init
    smtplib.SMTP_SSL.__init__ = smtp_ssl_init
    socket.create_connection = create_connection
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex

    def uninstall():
        smtplib.SMTP.__init__ = originals["smtp_init"]
        smtplib.SMTP_SSL.__init__ = originals["smtp_ssl_init"]
        socket.create_connection = originals["create_connection"]
        socket.socket.connect = originals["connect"]
        socket.socket.connect_ex = originals["connect_ex"]

    return uninstall
