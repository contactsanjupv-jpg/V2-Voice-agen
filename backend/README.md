# Atla v2 — Backend

FastAPI backend for the Atla voice-AI-receptionist SaaS. Retell for
voice/telephony, Google Calendar for booking (v1), PostgreSQL,
Redis-backed sessions.

**Status:** foundation + core Retell/booking provider layer is built and
tested (see below for exactly what). Onboarding orchestration, the full
calls/leads/appointments/billing/admin route layer, and everything
frontend are not built yet — see the architecture doc for the phased plan.

## What's real right now

- Auth (signup/login/logout/me), Argon2id + Redis sessions, rate limiting
- Multi-tenancy + RBAC, enforced server-side, integration-tested
- Full DB schema, migrated via Alembic
- SSRF-safe fetcher (ready to wire into the website importer)
- Retell provider layer: voices, phone numbers, agents, calls (real
  endpoint calls — untested against Retell's actual servers in this repo
  since that requires a real API key, but built exactly to their
  documented contracts)
- Google Calendar booking provider + race-condition-safe booking service
- Retell webhook endpoint: real signature verification + idempotency
- 33 automated tests, all passing against a real local Postgres + Redis

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

6. **Run tests:**
   ```bash
   python -m pytest tests/ -v
   ```

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

Not yet documented — no deployment target has been chosen. When one is,
this section covers: environment variable management in production,
running Alembic migrations as a release step (not ad hoc against prod),
and the background-worker process (`app/workers/`) needs a real queue
(RQ/Celery/arq) instead of the synchronous stand-in used in local dev.
