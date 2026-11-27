# Task 11 — security, integration and launch-readiness audit

Audited: `review/atla-platform-updates` @ `20848c7` (backend byte-identical to the Tasks 3–10 tree). Framework: OWASP ASVS (principal) and the
API Security Top 10 as a checklist. This is an engineering audit, not a legal opinion and not a penetration test.

## Findings

| # | Severity | Component | Finding | Status |
|---|---|---|---|---|
| F1 | Medium | `requirements.txt` (PyJWT 2.10.1, verifies the Google ID token) | 22 published advisories (pip-audit). Atla pins RS256, never uses `PyJWKClient`, mixed HMAC/RSA or detached JWS, so the headline issues are not reachable, but the library sits on the sign-in path. | **Fixed**: PyJWT 2.15.1; pip-audit now "No known vulnerabilities"; Google tests pass. Regression: `test_pyjwt_is_at_or_above_the_first_release_without_known_advisories` (failed before). |
| F2 | Medium | `LoginRequest.password` | No length limit on an unauthenticated endpoint: multi-MB passwords went to Argon2 (CPU/memory abuse; rate limits only count attempts). | **Fixed**: `max_length=128` (same as signup). Regression: `test_login_rejects_oversized_passwords_before_any_hashing` (failed before: 401). |
| F3 | Low | Frontend tests on the branch | `notifications.test.ts` used `URL.pathname` (breaks on a path containing spaces). | **Fixed** (`fileURLToPath`). |
| F4 | Low | Repo hygiene | `monetization-fix*.patch` committed at repo root. | Open: delete them (no secrets found). |
| F5 | Low | `docker-compose.yml` | Publishes Postgres/Redis with default credentials and no Redis auth. Dev only. | Open: never use on a server; documented here. |
| F6 | Low | `destroy_all_sessions_for_user` | Scans every session key (O(total sessions)) on password change/reset/delete. Fine at launch scale. | Open: add a per-user session index before ~10k concurrent sessions. |
| F7 | Medium (privacy) | Calls/transcripts/recordings | No retention or pruning policy for calls, transcripts, summaries or recording URLs (only notifications have 180 days). | Open: needs a product/legal decision, then a pruning job. |
| F8 | Medium (operational) | App-wide | No request-body size limit in the app; error tracking (Sentry etc.) not integrated. | Open: set a body limit at the proxy; add error tracking. |

No Critical or High defect was found in the code paths examined.

## What was examined, and how

* **Route table (77 routes), scripted:** every organization route depends on `current_membership`; the remainder are an explicit allow-list (auth, webhooks, health, public catalog). `/health/alerts` needs a bearer token; `/api/v1/dev/simulate-inbound` needs a session and is 404 outside the local sandbox guard.
* **Authentication:** Argon2id; opaque signed server-side sessions in Redis; cookie HttpOnly, SameSite=Lax, Secure in production; reset tokens 256-bit, hashed at rest, single-use (atomic GETDEL), replaced by a newer one; every session revoked on reset and change; login and reset answer identically for known and unknown addresses; Google ID token verified RS256-only with iss/aud/exp/nonce/verified-email.
* **Adversarial probes (new, 15 tests in `tests/test_t11_security_probes.py`):** forged/tampered/foreign-secret cookies, signed cookie with no server session, logout replay, CSRF with text/plain, form and multipart content types, CORS origin matrix, security headers, user enumeration, reset-token replay and junk, malformed IDs and injection strings (no 5xx, no internals), unsubscribed orgs refused before provider calls, oversized password, PyJWT floor.
* **Tenant isolation:** the existing route-driven suite (every org route called by another tenant; every by-id route with foreign ids). Not re-written.
* **Billing/credits:** existing tests re-run: webhook signature/replay/skew, concurrent redelivery grants once, forged organization or amount grants nothing, refund/chargeback/pending/rejected adjustments, reconciler. Paddle handler reviewed: record and processing share one transaction, so a failed run is retried rather than dropped.
* **Migrations:** scratch DB base→head, `alembic check` clean, single head `c3e5a7b92d14`, downgrade to base (0 tables left), re-upgrade, `alembic check` clean, 40 foreign keys. Scratch DB dropped.
* **Dependencies:** `npm audit --omit=dev`: 0. `pip-audit`: see F1.
* **Secrets:** pattern scan of tracked files found only fake test values; `.env` ignored in both backend and frontend.

## Launch-readiness matrix

| Area | Result | Evidence |
|---|---|---|
| Authentication | PASS (local) | Probes + existing auth, reset and Google suites; F1/F2 fixed |
| Authorization and tenant isolation | PASS (local) | Route-table scan; route-driven isolation suite |
| Billing and credit integrity | PASS (local) / BLOCKED live | Existing ledger/webhook/concurrency tests; no Paddle sandbox credentials here |
| Retell integration | PASS (local) / BLOCKED live | Signature and lifecycle tests; no Retell sandbox here; number purchases untested live by design |
| Notifications and workers | PASS (local) | Tasks 3–10 suites, policy guard, email probe log absent |
| Database and migrations | PASS (scratch DB) | Round trip above; target-DB run and restore drill remain with the operator |
| Frontend | PARTIAL | typecheck, lint, 218 unit/source tests pass (backend: 2136 passed incl. 15 new probes). **Production build: completed successfully on the owner's Mac (TypeScript and all 26 static pages), as reported by the owner; it cannot run in the audit sandbox because Google Fonts is blocked.** **No browser/e2e tests exist**, so browser behavior is still unverified |
| Privacy and data handling | FAIL (open) | F7; recording status unverified (docs/DISCLOSURE.md); legal review needed |
| Production configuration | NOT VERIFIED | Start-up guard tested; real env, TLS, `FORWARDED_ALLOW_IPS`, SMTP unverified |
| Monitoring and recovery | PARTIAL | Health/alerts built and tested; monitor, alert webhook, supervisor, error tracking, backup schedule not in place |

## Verification status by evidence level

* **Locally verified:** everything marked "local" above.
* **Provider sandbox:** none. No Paddle or Retell sandbox credentials were available; nothing here is a live test.
* **Real production credentials:** none used.
* **Not verified:** live checkout/refund/chargeback, Retell inbound/transfer/recording, number provisioning on a real account, Google OAuth against real Google, real SMTP delivery of the allowed emails, browser behavior (no browser/e2e tests), host backups/restore, rate limiting behind the real proxy.
* **Manual procedures:** docs/PADDLE_SANDBOX_TEST.md, docs/RETELL_VERIFICATION.md, docs/DEPLOYMENT.md, docs/LAUNCH_CHECKLIST.md.

## Matters for legal or provider confirmation (not engineering findings)

Call-recording and AI-disclosure consent by state (two-party-consent states), whether Retell records audio on this account, transcript retention and deletion promises, telemarketing rules if outbound is ever added, Paddle/Retell data-processing terms, privacy policy text.

## Decision: CONDITIONAL GO (controlled pilot only)

No unresolved Critical or High code defect was found. It is not a GO because mandatory gates lack evidence. Conditions, owner Sanju, all **before the first paying customer**:

1. Run the Paddle sandbox flow (docs/PADDLE_SANDBOX_TEST.md) including cancel and refund, and the Retell steps that cost money, and record the results.
2. A browser smoke test of login, Google sign-in, reset, the bell drawer and preferences (the production build itself is done: it passed on the owner's Mac).
3. Decide and implement transcript/recording retention (F7); confirm Retell recording setting; get legal sign-off on consent language.
4. Set `FORWARDED_ALLOW_IPS` to the proxy, a proxy body-size limit, TLS/HSTS, and verify two networks are rate-limited independently.
5. Configure the uptime monitor, `MONITORING_TOKEN` alert webhook, worker supervisor, error tracking and a restored-backup drill.
6. Delete the stray patch files (F4).
