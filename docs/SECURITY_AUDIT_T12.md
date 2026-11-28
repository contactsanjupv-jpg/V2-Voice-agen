# Task 12 — production security and launch-readiness: what was closed, what remains

Baseline: `review/atla-platform-updates` @ `8ad6215` (Task 11). Everything below is on top of it. Nothing was deployed, no provider was called, no real
customer data was touched, `main` was not modified.

## 1. Findings and severity

| # | Severity | Finding | Status |
|---|---|---|---|
| T12-1 | Medium (privacy) | No retention or pruning for calls, transcripts, summaries, recording keys or caller numbers | **Fixed in code** (off until you set the periods) |
| T12-2 | Medium (privacy) | Retell keeps recordings/transcripts forever by default and Atla never set its per-agent retention | **Mitigation added** (`RETELL_DATA_RETENTION_DAYS`); **not verified live** |
| T12-3 | Medium | No request-body size limit in the application | **Fixed** |
| T12-4 | Medium | Production accepted `FORWARDED_ALLOW_IPS='*'` (spoofable client IP → rate-limit bypass), a short `MONITORING_TOKEN`, non-https alert/DSN URLs | **Fixed** (production refuses to start) |
| T12-5 | Medium | No error tracking | **Integrated** (optional Sentry, privacy-scrubbed); **not verified against a real project** |
| T12-6 | Medium | No real-browser tests; frontend verified only by source-inspection tests | **Added 11 browser tests**; billing/cancel/Google/receptionist-live flows not coverable without provider credentials |
| T12-7 | Low | Backup/restore drill was a manual procedure | **Scripted** (`backend/scripts/backup_restore_drill.sh`); exercised on an essentially empty migrated schema only |
| T12-8 | Low | Stray `monetization-fix*.patch` files in the repo root | **Removed** |
| T12-9 | Low (found by my own mutation testing) | A retention batch whose update silently did nothing would loop forever | **Fixed** (identical-batch guard) |
| T12-10 | Low (test infrastructure) | A killed test run leaves rows in the test database, so the next full run can fail with `UniqueViolation` until the database is reset | Open; documented, not a product issue |

No Critical or High defect was found or remained from Task 11.

## 2. Exact fixes
* **Retention** (`app/services/retention.py`, model `DataRetentionRun`, migration `d4f6b8c03e25` chaining from `c3e5a7b92d14`, worker job `data_retention`):
  `TRANSCRIPT_RETENTION_DAYS` deletes the transcript and clears summary and recording key; `CALLER_DATA_RETENTION_DAYS` clears the caller number; the call row
  (time, duration, status, cost) is kept for billing. Every statement is scoped by organization; one transaction per organization; failures are rolled back, recorded
  and retried; batches; idempotent; audit rows hold counts only. Settings are range-validated. Details and the decisions left to you: `docs/DATA_RETENTION.md`.
* **Retell retention**: `data_storage_retention_days` added to the agent payload only when `RETELL_DATA_RETENTION_DAYS` is set (1–730, per Retell's documentation).
* **Body limit** (`app/core/body_limit.py`, pure ASGI, outermost): 413 on declared or streamed size over `MAX_REQUEST_BODY_BYTES` (1 MiB) or `MAX_WEBHOOK_BODY_BYTES` (10 MiB on `/webhooks/*`).
* **Fail-closed production** (`config.production_config_problems`): `FORWARDED_ALLOW_IPS='*'`, `MONITORING_TOKEN` < 32, non-https `ALERT_WEBHOOK_URL`/`SENTRY_DSN` refuse start-up (names only, never values).
* **Error tracking** (`app/core/error_tracking.py`): off without a DSN; no PII, bodies, cookies, headers, query strings, user records or local variables; emails, numbers, bearer/long tokens redacted; start-up never fails because of it; unhandled API errors and failed worker jobs are reported.
* **Browser tests** (`frontend/e2e/browser.test.mjs`, `docs/E2E_TESTING.md`, `npm run e2e`, devDependency `playwright@1.63.0`).
* **Docs**: `DATA_RETENTION.md`, `E2E_TESTING.md`, `DEPLOYMENT.md` §7, `MONITORING.md`. `.env.example` lists the new settings.

## 3. Tests and actual results
| Check | Result |
|---|---|
| Full backend suite (`atla_test`, clean DB) | **2180 passed, 0 failed** (7m17s; SMTP probe log absent) |
| New backend tests | 22 retention, 20 production hardening/error tracking, 2 worker isolation, plus the Task 11 probes (15) |
| Frontend typecheck / lint / `npm test` | exit 0 / exit 0 / 218 of 218 |
| Browser tests (real Chromium + real Next dev server + real API + Postgres + Redis, scratch DB) | **11 of 11 passed** |
| `ruff check .` | all checks passed |
| `pip-audit -r requirements.txt` | no known vulnerabilities (includes `sentry-sdk` 2.77.0) |
| `npm audit --omit=dev` | 0 vulnerabilities |
| Alembic (scratch DB) | single head `d4f6b8c03e25`; upgrade; `alembic check` clean; downgrade one step (table removed, `notification_preferences` kept); re-upgrade; downgrade to base (0 tables); upgrade; `alembic check` clean |
| Mutation checks | retention: 7 mutants, 4 killed, 3 equivalent (defence-in-depth org filter, redundant gate) and one gap closed with a new test; body limit and scrubber: 5 mutants, all killed after strengthening one test |
| Smtp probe log | absent (no email attempt) |
| Real network used | none: no provider, no email, no payment |
| Production build | not re-run here (Google Fonts blocked in this sandbox); passed on the owner's Mac for Task 11 and this change adds no routes or pages. **Re-run it on your Mac for this commit.** |

Task 11's oversized-password test was updated, not weakened: a 2 MB body is now refused earlier by the body limit (413) instead of by validation (422); 129 and 100 000 characters still get 422.

## 4. Remaining blockers (and why they remain)
1. **Paddle**: checkout, activation, upgrade/downgrade, cancel, refund, chargeback against a real sandbox. No sandbox credentials here. Procedure: `docs/PADDLE_SANDBOX_TEST.md`.
2. **Retell**: inbound call, transfer, recording setting, number purchase/adoption/release, the new `data_storage_retention_days` field accepted by Retell. No credentials; purchases cost money. Procedure: `docs/RETELL_VERIFICATION.md`.
3. **Google sign-in** against real Google (redirect URI and consent screen).
4. **Browser tests for billing, cancellation, receptionist go-live and Google** need the providers above; they are not faked.
5. **Production infrastructure**: TLS/HSTS on the real host, trusted-proxy address, proxy body limit, database TLS, Redis auth/private network, secret rotation, worker supervisor and restart policy, uptime monitor, alert webhook delivery, backup schedule and a timed restore of real data.
6. **Sentry**: a real project and a staging test event to confirm no personal data appears.
7. **SMTP**: real delivery of the two allowed email kinds (password reset, security alert).

## 5. Live-provider verification status
Locally verified with fakes/in-process only: Paddle webhooks and ledger, Retell webhooks and provisioning, Google token verification. **Verified with provider sandbox: none. Verified with production credentials: none.** Nothing here is a live test.

## 6. Production infrastructure verification status
Not verified (no access). What the code now enforces at start-up is listed in `docs/DEPLOYMENT.md` §7; whether the real host satisfies it is unproven.

## 7. Decisions that need you
Retention periods (transcripts/summaries, caller numbers, leads); whether to record audio and the consent wording per state; the AI-disclosure text; what the privacy policy promises; processor terms with Retell and Paddle; whether Retell recording is enabled on your account. I make no legal-compliance claim.

## 8. Launch decision: **CONDITIONAL GO — controlled pilot only**
No unresolved Critical or High finding. It is not a GO: the money path and telephony have never run against a real sandbox, infrastructure is unverified, and the retention periods and consent wording are undecided. Conditions (owner: you), all before the first paying customer: blockers 1, 2, 3, 5, 6 and 7 above, the §7 decisions, and a Mac production build of this commit. Automated tests passing is not evidence for any of those.

## 9. Commit and files
Parent: `8ad6215`. A commit cannot contain its own hash, so the new SHA is reported in the hand-off message. Changed files (status, path):

```
  M	backend/.env.example
  A	backend/alembic/versions/d4f6b8c03e25_data_retention_runs.py
  M	backend/app/config.py
  A	backend/app/core/body_limit.py
  M	backend/app/core/error_handling.py
  A	backend/app/core/error_tracking.py
  M	backend/app/db/models/__init__.py
  A	backend/app/db/models/retention.py
  M	backend/app/main.py
  M	backend/app/providers/agent/retell_agent_provider.py
  A	backend/app/services/retention.py
  M	backend/app/workers/run_worker.py
  M	backend/requirements.txt
  A	backend/scripts/backup_restore_drill.sh
  A	backend/tests/test_production_hardening.py
  A	backend/tests/test_retention.py
  M	backend/tests/test_t11_security_probes.py
  M	backend/tests/test_worker_job_isolation.py
  A	docs/DATA_RETENTION.md
  M	docs/DEPLOYMENT.md
  A	docs/E2E_TESTING.md
  M	docs/MONITORING.md
  A	docs/SECURITY_AUDIT_T12.md
  A	frontend/e2e/browser.test.mjs
  M	frontend/package-lock.json
  M	frontend/package.json
  D	monetization-fix-on-8d5fa74.patch
  D	monetization-fix.patch
  A  docs/SECURITY_AUDIT_T12.md
```
