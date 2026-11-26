# Forgot password

Flow: `/forgot-password` → `POST /api/v1/auth/password-reset/request` (always 204) → email with `FRONTEND_URL/reset-password?token=…`
→ `/reset-password` → `POST /api/v1/auth/password-reset/confirm` → every session signed out → "password changed" email.

- Token: `secrets.token_urlsafe(32)`, only its SHA-256 is stored in Redis, TTL `PASSWORD_RESET_TTL_SECONDS` (default 3600), single use,
  a new request invalidates the previous link.
- Limits: 10 requests/hour/IP and 3/hour/email (silently still 204), 20 confirms/hour/IP (429).
- Emails are sent after the response, so timing and status never reveal whether an account exists.
- Email bodies are never logged; addresses are masked (`j***@example.com`).

## Why it "did nothing"
Without `SMTP_HOST` no email can be sent. The API still answered 204 (by design), so it looked like it worked. In development the old
code logged the whole message, including the reset link. Set the SMTP variables below.

## Configure (backend/.env)
```
FRONTEND_URL=https://app.yourdomain.com     # must be https in production; it is the base of the reset link
SMTP_HOST=...  SMTP_PORT=587  SMTP_USERNAME=...  SMTP_PASSWORD=...
SMTP_FROM=Atla <no-reply@yourdomain.com>    # a sender/domain your provider has verified (SPF/DKIM) or mail lands in spam
SMTP_STARTTLS=true                          # port 587
SMTP_USE_SSL=false                          # or true / port 465 for implicit TLS
```
Restart the backend (settings are cached).

## Prove delivery
```
cd backend
python -m app.workers.send_test_email --to you@yourmail.com
```
Exit 0 means the SMTP server accepted it; then confirm it arrives (check spam). Then do the real flow in the browser.

## Local development without a provider
Run Mailpit (`docker run -p 8025:8025 -p 1025:1025 axllent/mailpit`), set `SMTP_HOST=localhost SMTP_PORT=1025 SMTP_STARTTLS=false`,
and read mail at http://localhost:8025.
