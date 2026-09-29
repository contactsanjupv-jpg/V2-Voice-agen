# Security

This document describes what's actually implemented, where, and — just as
important — what is **not yet** implemented. Nothing here claims the
system is "hack-proof." Treat every unchecked item as a real gap, not a
formality.

## Authentication

- Passwords hashed with **Argon2id** (`app/auth/passwords.py`, via
  `argon2-cffi`). No homemade crypto.
- Sessions are server-side, stored in Redis, keyed by an opaque
  cryptographically random ID. The cookie holds only that ID, signed
  (`itsdangerous`) so it can't be forged, `HttpOnly`, `SameSite=Lax`, and
  `Secure` in production. Server-side sessions mean logout / "revoke all
  sessions" is instant — no JWT-denylist workaround needed.
- Login and signup are rate-limited per-IP **and** per-email
  (`app/auth/rate_limit.py`). Failed login against a nonexistent email
  still runs a dummy Argon2 verify so response timing doesn't leak whether
  the account exists.
- **Not yet implemented:** email verification enforcement (the field
  exists on `User`, nothing currently gates on it), password reset flow,
  "log out all other sessions" UI, 2FA.

## Multi-tenancy & authorization

- `organization_id` for authorization is **always** resolved from the
  authenticated session's membership row (`app/auth/deps.py:
  current_membership`), never trusted from a URL/body parameter directly.
  A request for another org's resources returns **404**, not 403 — we
  don't confirm the resource exists to a non-member.
- RBAC (`owner` > `admin` > `member`) enforced via `require_role()` on
  every mutating endpoint — never a frontend-only check.
- Verified with real integration tests
  (`tests/test_tenant_isolation.py`) that attempt actual cross-tenant
  reads/writes over HTTP against a live database and assert every one
  fails.
- **Not yet implemented:** an actual member-invite flow (currently only
  the signup-time `owner` role is created), org-level API key issuance
  for programmatic integrations.

## SSRF (website importer)

`app/core/ssrf_safe_fetch.py`: DNS pre-resolution and validation before
connecting, rejects private/loopback/link-local/multicast/reserved
ranges and known cloud-metadata hosts, connects to the pre-validated IP
directly (closing the DNS-rebinding TOCTOU gap), re-validates on every
redirect hop, enforces size/time/redirect-count/content-type limits.
Tested in `tests/test_ssrf_protection.py`, including a simulated
DNS-rebind scenario.

## Retell webhook verification

`app/webhooks/retell_signature.py` implements Retell's actual documented
scheme: `HMAC-SHA256(raw_body + timestamp, key=RETELL_API_KEY)`, header
format `v={timestamp},d={digest}`, verified against the **raw** body
(never a re-serialized copy), constant-time comparison, and a 5-minute
replay window. Verified against a duplicated event
(`app/db/models/platform.py: WebhookEvent` — `UNIQUE(source,
external_event_id)`) — a retried delivery is a no-op, not a duplicate
lead/appointment. Tested in `tests/test_webhook_signature.py`.

## Booking race conditions

`app/services/booking_service.py`: never reports a booking as successful
before the provider confirms it; re-checks availability immediately
before writing; on a provider-level conflict (Google Calendar 409),
re-queries real availability and returns real alternatives — never
invents one. Tested in `tests/test_booking_race_condition.py`, including
a simulated "taken between check and write" race.

## Secrets

`RETELL_API_KEY`, `TOKEN_ENCRYPTION_KEY`, `APP_SECRET_KEY`,
`GOOGLE_OAUTH_CLIENT_SECRET`, `DATABASE_URL` — env vars only
(`app/config.py`), never a frontend-reachable variable, never logged.
Integration OAuth tokens are encrypted at rest with Fernet
(`app/core/crypto.py`) and only decrypted inside the relevant
`BookingProvider` adapter, immediately before use.

## Error handling

Unhandled exceptions never reach the client with a stack trace or
internal detail (`app/core/error_handling.py`) — a generic message plus a
correlatable `error_id` is returned; the real detail is logged
server-side only.

## Security headers

`app/core/security_headers.py`: CSP, `X-Content-Type-Options`,
`X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`, and HSTS
in production. CSP is currently strict (`default-src 'self'`) — will need
loosening for specific third-party assets (fonts, analytics) as the
frontend is built; loosen deliberately, one directive at a time, not with
a blanket `unsafe-inline`/`unsafe-eval`.

## Known gaps (tracked, not yet built)

- CSRF: cookies are `SameSite=Lax`, which blocks the most common
  cross-site POST vector, but there's no CSRF token yet for
  state-changing requests. Add before handling anything beyond the
  current same-origin frontend assumption.
- No IDOR test coverage yet for `leads`/`appointments`/`calls` endpoints
  (those routes aren't built yet — extend `test_tenant_isolation.py` the
  moment they are, before shipping them).
- No audit-log writes yet (the `AuditLog` table exists; nothing writes to
  it — wire it into every RBAC-gated mutation as those endpoints are
  built).
- No automated dependency vulnerability scanning configured.
- Admin area (`AdminUser`) has a DB model and a `require_platform_admin`
  dependency but no routes yet.
