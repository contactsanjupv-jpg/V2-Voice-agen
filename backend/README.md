# Atla v2 — Backend

FastAPI backend for the Atla voice-AI-receptionist SaaS. Retell for
voice/telephony, Google Calendar for booking (v1), PostgreSQL,
Redis-backed sessions.

**Status:** the product is built end to end: sign-up and tenancy, onboarding, receptionist configuration,
go-live, Paddle billing and credits, Retell call processing, the dashboard APIs, and the background worker. What is
proven by automated tests (against fake providers) and what still needs a live-provider check is tracked in
`../docs/LAUNCH_CHECKLIST.md`; do not read "built" as "verified against live Retell/Paddle".

## What's built

- Auth: sign-up, login, logout, `me`, password change and reset (emailed, single-use, expiring tokens), Argon2id
  hashing, Redis-backed sessions, rate limiting.
- Multi-tenancy: every `/api/v1/orgs/{organization_id}/...` route sits behind `current_membership` (the caller must be
  a member of that organization), and every lookup by id is filtered by the caller's organization. A test walks the
  real route table to enforce this. Roles exist, but there is no invite flow yet: every account owns its own
  organization (see `SECURITY.md`).
- Onboarding: SSRF-safe website importer, business profile review, voice catalog, receptionist (agent) configuration
  and test calls.
- Go-live: phone-number provisioning (purchase is made durable and recoverable, never assumed), activation only after
  the provider-side assignment is verified, release rules for lapsed accounts.
- Billing: Paddle checkout, `POST /webhooks/paddle` (signature, de-duplication, ordering), subscriptions, cancellation,
  monthly included credits and purchased top-ups on a ledger, admin-only audited refunds, scheduled reconciliation.
- Calls: `POST /webhooks/retell` (signature-verified, stored before processing, retried with back-off, dead-lettered
  after repeated failure), calls, transcripts (Growth plan), leads, usage and cost accounting, dashboard and analytics.
- Entitlement enforcement: a subscription lapse, or credit spent beyond the allowed overage (including during the
  past-due grace period), pauses the receptionist; numbers are released later by policy (`../docs/LAPSE_POLICIES.md`).
- Operations: `/health`, `/health/ready`, token-protected `/health/alerts`, worker heartbeat and watchdog
  (`../docs/MONITORING.md`).
- Google Calendar: the booking provider and race-safe booking service exist; the OAuth connect/callback route does
  not (see "Google Calendar setup").

## Local setup

1. **Start Postgres + Redis:**
   ```bash
   docker compose up -d
   ```
   (Or install/run them natively — anything that satisfies
   `DATABASE_URL` / `REDIS_URL` in `.env` works.)

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt -r requirements-dev.txt
   ```

3. **Configure environment:**
   ```bash
   cp .env.example .env
   ```
   Fill in `APP_SECRET_KEY` and `TOKEN_ENCRYPTION_KEY` (generation
   commands are in the comments in `.env.example`). `RETELL_API_KEY` and
   the Google OAuth credentials can stay empty for auth/tenant-isolation
   work; they're required the moment you touch anything under
   `app/providers/`.

4. **Run migrations:**
   ```bash
   alembic upgrade head
   ```

5. **Run the server:**
   ```bash
   uvicorn app.main:app --reload
   ```
   API docs at `http://localhost:8000/docs` (disabled automatically when
   `ENV=production`).

6. **Run tests.** The suite runs against a real Postgres and Redis and DROPS ALL TABLES when it finishes, so it
   refuses to start unless the database name contains `test`. Create a throwaway database and point the run at it
   (use a separate Redis database number too, so the tests never touch your development keys):
   ```bash
   docker compose exec postgres createdb -U atla atla_test
   DATABASE_URL="postgresql+psycopg://atla:atla@localhost:5432/atla_test" \
   REDIS_URL="redis://localhost:6379/5" \
   python -m pytest tests/ -q
   ```
   No real Retell, Paddle or SMTP credentials are needed: provider calls are faked in the tests.

## Retell setup

1. Create a Retell account, generate an API key with webhook signing
   enabled (the same key signs webhook payloads — see `SECURITY.md`).
2. Set `RETELL_API_KEY` in `.env`.
3. Point Retell's webhook URL (per-agent `webhook_url`, set automatically
   by `agent_service.create_or_update_agent`) at
   `https://your-domain/webhooks/retell`. For local development, use a
   tunnel (ngrok or similar) — Retell needs to reach a public URL.
4. **Before commercializing:** read the Retell reseller/partner note in
   `THREAT_MODEL.md` and the architecture doc. This is a real open item,
   not a formality.

## Paddle setup (sandbox first)

1. In Paddle (sandbox), create the Starter and Growth prices and an API key, and a webhook endpoint pointing at
   `https://your-domain/webhooks/paddle`.
2. Set `PADDLE_ENV`, `PADDLE_API_KEY`, `PADDLE_WEBHOOK_SECRET`, `PADDLE_STARTER_PRICE_ID` and
   `PADDLE_GROWTH_PRICE_ID` in `.env`. The frontend needs the matching client-side token (see `../frontend/README.md`);
   the two environments must agree or checkout refuses to open.
3. Walk through `../docs/PADDLE_SANDBOX_TEST.md` before using live credentials.

## Google Calendar setup

1. In Google Cloud Console, create OAuth 2.0 credentials (Web application
   type), enable the Calendar API.
2. Set `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` /
   `GOOGLE_OAUTH_REDIRECT_URI` in `.env`.
3. The OAuth connect/callback route (`app/api/v1/integrations/`) is not
   built yet — `GoogleCalendarProvider` in
   `app/providers/booking/google_calendar_provider.py` is ready to be
   handed a valid access token once that route exists.

## Project layout

See `docs/architecture.md` (or the architecture doc shared separately) for
the full folder structure and database schema reference — this README
covers running what exists, not re-deriving the design.

## Deployment

No hosting target or container image is committed to this repository; choose one, then:

- Set `ENV=production`. The API and the worker both refuse to start with an unsafe configuration (secret shorter than
  32 characters, non-`https://` frontend or backend URL, `PADDLE_ENV` not set explicitly, a sandbox Paddle environment
  without `ALLOW_SANDBOX_BILLING_IN_PRODUCTION=true`, or an empty Retell/Paddle key, webhook secret, price id,
  `TOKEN_ENCRYPTION_KEY` or `SMTP_HOST`). The error lists setting names only, never values.
- Run `alembic upgrade head` as a release step, not by hand against production. Try it on a copy of the production
  database first.
- Run the API and the worker as two separate, supervised processes with the same environment. The worker's watchdog
  exits the process if a pass stalls, so the supervisor must restart it.
- Then follow `../docs/LAUNCH_CHECKLIST.md` and `../docs/MONITORING.md` (uptime monitor, alert webhook,
  `MONITORING_TOKEN`).

## Running the background worker

Call-event retries, phone-number reconciliation, entitlement enforcement (lapsed subscriptions and exhausted
credit), top-up settlement, alerts, the voice catalog and Paddle reconciliation all run in one Postgres-backed worker
process next to the API (no separate queue service is needed). Each job runs in isolation, so one failing job cannot
stop the others. Without the worker, failed Retell events are never retried, lapsed customers are never paused and
their numbers are never released.

    alembic upgrade head
    uvicorn app.main:app --reload            # API
    python -m app.workers.run_worker         # worker (separate terminal / process)

Set `ENV=production` in production (secure cookies, API docs off).
