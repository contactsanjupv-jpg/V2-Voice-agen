# Notifications (Task 3: backend foundation)

## Channel policy
- **Email:** password recovery and security alerts ONLY (sent directly by the account / sign-in endpoints).
- **In-app:** everything else a customer needs to know: leads, credits, billing, payment failures, receptionist state (and later phone, call, analytics and product notifications).
- The two channels are independent. Creating an in-app notification never reads SMTP, Resend or the quota breaker; an email failure cannot block, undo or delay it; email is never a fallback for it.
- `owner_notifications.EMAIL_KINDS` is empty. `enqueue()` records the in-app notification and queues no email. Rows queued before this policy are converted to in-app notifications by the worker pass and then skipped (never emailed).
- There is no daily-report, digest, announcement or product-update email code; `tests/test_notification_policy.py` fails if one appears or if any new module starts sending mail.

## Data model (`notifications`, migration `b2d4f6a81c03`, after `a1c3e5f70b92`)
One row per **recipient**: organization, recipient user, kind, category, severity, title, body, link, dedupe key, ids-only payload, `read_at`, timestamps, and a database-assigned `seq` (stable paging cursor, cheap "anything new?" check).
- `UNIQUE(organization_id, recipient_user_id, kind, dedupe_key)`: a repeated cause is a database no-op.
- CHECK constraints: closed category and severity vocabularies; a link may only be a relative `/dashboard…` path (no scheme, host, `//`, `..` or spaces).
- Indexes: `(recipient, organization, seq)` for paging; partial `(recipient, organization) WHERE read_at IS NULL` for unread counts.
- Audience is fixed at creation: owners and admins for billing/credits/service state; every member for leads. A member added later does not receive earlier notifications.

## Creation
`services/notifications.create()`: one `INSERT … SELECT` over the audience with `ON CONFLICT DO NOTHING`, inside a savepoint, in the caller's transaction. It commits or rolls back with the cause, never raises, and cannot poison the caller. Title/body/link come from a fixed catalogue; a lead's name is reduced to one printable line and nothing else of the customer is stored or shown.
Existing producers (call ingest, entitlement, credit sweeps, subscription sync) are unchanged: they already call `enqueue()`.

## API (all under `/api/v1/orgs/{organization_id}/notifications`, session cookie, `current_membership`)
`GET ""` (limit ≤ 50, `before` cursor, `unread_only`, `category`) · `GET /state` · `POST /{id}/read` · `POST /read-all[?category=]` · `GET /stream` (Server-Sent Events).
Every query is scoped to (caller, organization of the verified membership). Another tenant's organization, another member's notification and an unknown id all answer 404.

## Real time
Server-Sent Events: one open request per tab, authenticated by the same cookie, no new infrastructure. The server checks one indexed query every 3 s and pushes only when the newest notification or the user's unread count changes, closes after 5 minutes (the browser reconnects, re-checking session and membership), and a user may hold 5 streams per process. A client can fall back to polling `/state` if the stream is down.
Trade-off: the server polls the database per connection instead of subscribing to a broker. That is cheap at this scale and needs no Redis pub/sub or cross-process fan-out; a broker is the upgrade path if connections grow into the thousands. Per-user stream limits are per process.

## Guarantees and limits
- Database side: a notification exists if and only if its cause committed, and at most one exists per (recipient, kind, dedupe key). Read-state changes are single statements.
- Not claimed: delivery to a screen. The row is durable; whether a browser sees it depends on the browser.
- Retention: 180 days, pruned hourly by the worker.
- Notification text is generic by design; no transcripts, phone numbers or reasons.
