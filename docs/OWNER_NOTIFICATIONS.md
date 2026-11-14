# Owner notifications (Level 8A)

The business owner gets an email when something happens that they must act on. Before this, the only email Atla sent was
password reset, so a captured lead or a paused receptionist was visible only if the owner opened the dashboard.

| Email | Sent when | Not sent when |
|---|---|---|
| **New lead** | A real call captured a lead | The lead came from a browser test call; the call captured no lead |
| **Receptionist paused** | The entitlement pass actually switched the receptionist off (credits spent past the grace, payment still failing after the grace, subscription ended). The email gives the reason and how to resume. | Nothing was live to switch off (no repeat on later passes) |
| **Low minutes** | A paying customer's live receptionist is under the low-credit line (20% of the period's minutes, at least 10 minutes). Once per billing period. | Healthy balance; not subscribed; already paused |
| **Out of minutes** | Minutes are used up but the receptionist is still inside the overage grace. Once per billing period. | Minutes were added before it was sent; already paused |
| **Payment failed** | Paddle moves the subscription into `past_due`. States when service will stop. | Redelivered or repeated `past_due` snapshots; the customer paid before the email was sent |

Recipients: the organization's **owners and admins** (members are not emailed). Resolved when the email is sent.

## How it works

`owner_notifications` is a durable outbox. The cause (lead, pause, `past_due` transition) writes one row **in its own
transaction**; the worker (`app/workers/run_worker.py`) sends it every pass and retries a refused send with backoff
(1 min doubling to 1 h, `OWNER_NOTIFICATION_MAX_ATTEMPTS` = 8, then `failed`). A webhook never waits for SMTP.
`UNIQUE(organization_id, kind, dedupe_key)` is the "tell them once" rule. Delivery is at-least-once: a crash between SMTP
accepting a message and the commit can send one email twice.

Rows hold ids only. The message is built from live data when sent, so a notification that is no longer true is marked
`skipped` instead of sent.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `OWNER_NOTIFICATIONS_ENABLED` | `true` | Kill switch for queueing and sending |
| `OWNER_NOTIFICATION_MAX_ATTEMPTS` | `8` | Sends before a notification is marked `failed` |
| `CREDIT_ALERT_INTERVAL_SECONDS` | `300` | How often the worker checks every live organization's credit level |

Needs the existing `SMTP_*` settings (already required when `ENV=production`) and `FRONTEND_URL` for the links.

## When something is wrong

The monitoring alert `owner_notifications_undelivered` (critical) fires when an email is still unsent after 15 minutes of
retrying, or gave up in the last 7 days. Check SMTP first. Inspect:

```sql
select kind, status, attempts, last_error, created_at from owner_notifications
where status in ('pending','failed') order by created_at desc limit 50;
```

To resend a `failed` one after fixing SMTP: `update owner_notifications set status='pending', attempts=0, next_attempt_at=null where id = '<id>';`

## Known limits

- Email only (no SMS or push), and no per-owner opt-out. Use `OWNER_NOTIFICATIONS_ENABLED` to stop all.
- Times in emails are UTC. Business-local time arrives with Level 8B.
- The low/empty credit email is once per billing period. A customer who tops up and runs low again in the same period is not warned a second time.
- A failed *top-up* payment is not announced (a top-up is paid interactively at checkout).
- A lead email contains the caller's name, callback number and stated reason, sent over your SMTP provider. No transcript.
