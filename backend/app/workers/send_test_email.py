"""Send ONE harmless test email through the configured SMTP settings, to prove real delivery.

    python -m app.workers.send_test_email --to you@example.com

Exit code 0 only if the SMTP server accepted the message (that is not proof the inbox received it:
check the inbox, and the spam folder). Prints setting NAMES and the outcome, never credentials."""
import argparse
import sys

from app.config import get_settings
from app.core.email import in_test_mode, send_email


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send a test email using the configured SMTP settings.")
    parser.add_argument("--to", required=True, help="recipient address")
    args = parser.parse_args(argv)
    settings = get_settings()
    if in_test_mode():
        print("ENV=test: test mode never sends email, so nothing was sent. Run this with ENV=development (or production).")
        return 2
    if not settings.SMTP_HOST:
        print("SMTP_HOST is not set: nothing was sent. Configure the SMTP_* settings (docs/PASSWORD_RESET.md).")
        return 2
    mode = "implicit TLS" if (settings.SMTP_USE_SSL or settings.SMTP_PORT == 465) else (
        "STARTTLS" if settings.SMTP_STARTTLS else "no TLS"
    )
    print(f"Sending via {settings.SMTP_HOST}:{settings.SMTP_PORT} ({mode}), From: {settings.SMTP_FROM}")
    ok = send_email(args.to, "Atla test email", "If you can read this, Atla can send email. No action needed.")
    print("Accepted by the SMTP server. Now check the inbox (and spam)." if ok else "FAILED: see the log line above.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
