# Paddle sandbox: setup and end-to-end test

Sandbox only. Never put live keys in this environment. Do not paste credentials into chat or commit them.
Status: **NOT VERIFIED** - no sandbox credentials have been provided; nothing below has been run against Paddle.

## Setup (once)
1. Create a Paddle **Sandbox** account (sandbox-vendors.paddle.com). Create two recurring prices (Starter, Growth) and note the `pri_...` ids.
2. Developer tools > Authentication: create a sandbox API key (read/write: subscriptions, transactions, adjustments, customers, notifications).
3. Developer tools > Notifications: add a destination `https://<public-url>/webhooks/paddle` and subscribe to `subscription.*`, `transaction.*`, `adjustment.*`. Copy its secret key. For a local backend use a tunnel (ngrok/cloudflared) as the public URL.
4. Default payment link (Checkout settings) set to your frontend URL.
5. In an untracked `backend/.env.sandbox` (never committed) set: `PADDLE_ENV=sandbox`, `PADDLE_API_KEY`, `PADDLE_WEBHOOK_SECRET`, `PADDLE_STARTER_PRICE_ID`, `PADDLE_GROWTH_PRICE_ID`, `FRONTEND_URL`, `BACKEND_PUBLIC_URL`, `AUTO_PROVISION_NUMBERS=false` (so Retell is not touched). Use a separate database, not `atla_test` and not production.
6. Start API, worker and frontend. Sandbox test card: 4242 4242 4242 4242, any future expiry, any CVC.

## Test sequence and what to record
| # | Action | Expect | Record |
|---|---|---|---|
| 1 | Sign up, choose Starter, pay with the test card | `subscription.created/activated` webhooks 204; Settings shows active | raw webhook payload shape |
| 2 | Replay one webhook from the Paddle dashboard | 204, no duplicate rows or grants | |
| 3 | Settings > Cancel plan | 202 `requested` with `effective_at`; Paddle shows scheduled cancel; Settings shows "Ends on" | exact `scheduled_change` shape in GET /subscriptions/{id} and in `subscription.updated` |
| 4 | Cancel again | `already_scheduled`, no second cancel call in logs | |
| 5 | Confirm access continues | status active, features entitled | |
| 6 | Grant yourself platform admin (`docs/SANDBOX_PLATFORM_ADMIN.md`), then POST `/api/v1/admin/refunds` for the sandbox transaction | 202 `submitted` or `approved` | Paddle's response to `POST /adjustments` (status values, field names) |
| 7 | Approve/reject in the Paddle sandbox dashboard | `adjustment.updated` webhook moves our row to `approved`/`rejected`; audit rows written | payload shape |
| 8 | Repeat the refund request | 409 already exists, no second adjustment | whether Paddle itself also refuses a duplicate |
| 9 | Timeout recovery: with the request in flight, block outbound to api sandbox (or stop the tunnel/network) so the POST times out, then retry | row `unconfirmed`; retry adopts the existing adjustment (or re-posts only if none exists) | `GET /adjustments?transaction_id=&action=refund` response |
| 10 | Wrong webhook secret briefly | 401s; after 10/hour the `webhook_rejected_paddle` alert fires | |

After each step compare against the assumptions list in docs/CANCELLATION_AND_REFUNDS.md and fix any that differ, with a regression test.


## PADDLE_ENV safety
`PADDLE_ENV` (backend) selects the API host: `sandbox` → `https://sandbox-api.paddle.com`, `live` → `https://api.paddle.com`. Only those two values are accepted (anything else fails at startup). With `ENV=production` the app refuses to start unless `PADDLE_ENV` is set explicitly, is `live` (or `sandbox` with `ALLOW_SANDBOX_BILLING_IN_PRODUCTION=true` for a deliberate sandbox pilot), and the API key does not look like the other environment's key (name-only error; the `pdl_sdbx_`/`pdl_live_` prefix check is a best-effort heuristic to confirm against Paddle's docs).
The frontend has a separate `NEXT_PUBLIC_PADDLE_ENV` and client token that the backend cannot verify: set both together. A mismatch fails at checkout rather than charging.

## Environment consistency (backend is authoritative)
- `ENV` must be `development`, `test` or `production`; anything else fails startup (it used to silently disable the production guards).
- The API **and the worker** run the same production guard at startup and refuse to start on an unsafe configuration. Both publish a non-secret fingerprint (environment, Paddle mode, URLs, database host/name, price ids, lapse policy); a difference raises the `config_mismatch` alert.
- `GET /api/v1/public-config` returns `{"paddle_env": "sandbox"|"live"}` (no secrets). The frontend opens Paddle checkout in THAT environment and refuses to open it if its `NEXT_PUBLIC_PADDLE_CLIENT_TOKEN` does not match: `test_` for sandbox, `live_` for live (prefixes per Paddle's Paddle.js docs). `NEXT_PUBLIC_PADDLE_ENV` is no longer used.
- Still to verify in the sandbox: what Paddle.js does when token and environment disagree (undocumented; ours refuses before calling it).

## Unmetered browser test calls on the local sandbox clone (optional, local only)

A cloned sandbox database can have active subscriptions but no credits, so browser test calls return 402. For that
case only, add this line to your untracked `backend/.env.sandbox`:

```
ALLOW_UNMETERED_LOCAL_TEST_CALLS=true
```

In the same file, `DATABASE_URL` **must point to the database `atla_paddle_sandbox_clone`**, for example
`postgresql+psycopg://<user>:<password>@localhost:5432/atla_paddle_sandbox_clone`. Load `backend/.env.sandbox` for
**both** the API and the worker (the worker records the usage).

To test after-hours (the closed receptionist), also put `AFTER_HOURS_ROUTING_ENABLED=true` in that file. The app does not read
`.env.sandbox` by itself: export it or start with `--env-file`, and restart the backend. See `docs/AFTER_HOURS.md`.

The flag is refused (the API and worker won't start) unless **all** of these hold:
- `ENV=development`
- `PADDLE_ENV=sandbox`
- the database name is exactly `atla_paddle_sandbox_clone`
- the database host is a loopback address: `localhost`, `127.0.0.1` or `::1` (with no `?host=`, `?hostaddr=`,
  `?service=` or `?dbname=` override in the URL). A remote server is refused even if its database has that name.

What it does: skips the credit check and the exhausted free-test refusal for browser test calls. What it does not do:
grant credits, change subscriptions or billing rows, or skip login, membership, agent checks, the daily test-call
limit or the maximum call duration. A lapsed plan is still refused. Real Retell cost is still recorded, under its own
usage kind `dev_test` (never charged, never counted as the free allowance or customer-paid usage). A call admitted this
way stays `dev_test` even if the flag is later turned off before its webhook arrives.
