"""Minimal outgoing email via SMTP (stdlib only). Delivery is UNVERIFIED until
SMTP credentials are configured and one real reset email is received."""
import logging
import smtplib
from email.message import EmailMessage

from app.config import get_settings

logger = logging.getLogger("atla.email")


def send_email(to: str, subject: str, body: str) -> bool:
    """Returns True if handed to a mail server. Never raises: callers must not
    reveal (via errors or timing) whether an address has an account."""
    settings = get_settings()
    if not settings.SMTP_HOST:
        if settings.is_production:
            logger.error("SMTP is not configured: email to %s was NOT sent", to)
        else:
            logger.warning("DEV email to %s | %s\n%s", to, subject, body)
        return False
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = settings.SMTP_FROM, to, subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as smtp:
            if settings.SMTP_STARTTLS:
                smtp.starttls()
            if settings.SMTP_USERNAME:
                smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            smtp.send_message(msg)
        return True
    except Exception:  # noqa: BLE001
        logger.exception("Sending email to %s failed", to)
        return False
