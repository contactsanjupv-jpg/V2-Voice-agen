"""
Optional error tracking (Sentry). Off unless SENTRY_DSN is set; nothing is sent from tests or development without it.

Privacy rules, enforced in code and by tests/test_error_tracking.py:
  * no default PII, no request bodies, no cookies or auth headers, no query strings, no local variables in stack frames;
  * everything that is sent passes through `scrub_event`, which also redacts emails, phone numbers, bearer/hex tokens and
    card-like numbers inside exception messages, breadcrumbs and log messages;
  * the DSN is never logged.
Install-time failure of the SDK must never stop the application: initialisation is best effort.
"""
import logging
import re
from typing import Any

logger = logging.getLogger("atla.errors")

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"\+?\d[\d\s().-]{8,}\d")
_BEARER = re.compile(r"(?i)\b(bearer|token|secret|password|api[_-]?key|authorization)\b\s*[:=]?\s*\S+")
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")
_SENSITIVE_KEYS = ("authorization", "cookie", "set-cookie", "x-api-key", "x-retell-signature", "paddle-signature", "token", "password", "secret", "email", "phone", "transcript", "summary")

_initialised = False


def redact(text: str) -> str:
    text = _EMAIL.sub("[email]", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)}=[redacted]", text)
    text = _PHONE.sub("[number]", text)
    return _LONG_TOKEN.sub("[token]", text)


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "[truncated]"
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: ("[redacted]" if any(s in str(k).lower() for s in _SENSITIVE_KEYS) else _scrub(v, depth + 1)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v, depth + 1) for v in value]
    return value


def scrub_event(event: dict, hint: dict | None = None) -> dict | None:
    request = event.get("request")
    if isinstance(request, dict):  # keep method and path only; never the body, headers, cookies, query or caller address
        url = request.get("url", "")
        event["request"] = {"method": request.get("method"), "url": redact(str(url).split("?", 1)[0])}
    event.pop("user", None)
    event.pop("server_name", None)
    for key in ("extra", "contexts", "tags", "breadcrumbs", "exception", "message", "logentry"):
        if key in event:
            event[key] = _scrub(event[key])
    for exc in (event.get("exception") or {}).get("values", []) if isinstance(event.get("exception"), dict) else []:
        for frame in (exc.get("stacktrace") or {}).get("frames", []):
            frame.pop("vars", None)
    return event


def init_error_tracking(process: str) -> bool:
    """Start the SDK when SENTRY_DSN is set. Returns whether it is active. Never raises."""
    global _initialised
    from app.config import get_settings

    cfg = get_settings()
    if not cfg.SENTRY_DSN:
        if cfg.is_production:
            logger.warning("SENTRY_DSN is not set: unhandled errors in the %s are logged but not tracked", process)
        return False
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=cfg.SENTRY_DSN, environment=cfg.SENTRY_ENVIRONMENT or cfg.ENV, send_default_pii=False, include_local_variables=False,
            max_request_body_size="never", traces_sample_rate=0.0, before_send=scrub_event, before_breadcrumb=lambda crumb, hint: _scrub(crumb),
            server_name="",
        )
        sentry_sdk.set_tag("process", process)
        _initialised = True
        return True
    except Exception:  # noqa: BLE001 - error tracking must never stop the application
        logger.exception("Error tracking could not start; continuing without it")
        return False


def report_exception(exc: BaseException) -> None:
    if not _initialised:
        return
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
    except Exception:  # noqa: BLE001
        pass
