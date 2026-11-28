# Deploying Atla (what was exercised, what was not)

Each item says how it was checked. **Exercised** = run against real local processes (uvicorn, the worker, Postgres, Redis) with fake
secrets. **Not exercised** = needs your real host or provider accounts; do it before real customers.

## 1. Reverse proxy and client IPs (rate limiting)

Signup, login and password reset rate-limit on the caller's IP (`request.client.host`), and login also limits per email.
uvicorn only fills `client.host` from `X-Forwarded-For` when the **direct caller** is in `--forwarded-allow-ips`
(env `FORWARDED_ALLOW_IPS`, default `127.0.0.1`).

Exercised with three real uvicorn processes (login limit set to 3, each attempt using a different email so only the IP limit counts):

| Configuration | Client X fails 4 times | Client Y, a different customer |
|---|---|---|
| Proxy is a trusted caller (`FORWARDED_ALLOW_IPS` includes it) | 4th attempt gets 429 | **not affected** (401) |
| Proxy is NOT trusted (default on most hosted platforms) | 4th attempt gets 429 | **also blocked (429)**: every customer shares the proxy's IP |
| `FORWARDED_ALLOW_IPS='*'` (trust everyone) | 4th attempt gets 429 | not affected, but a client can send any `X-Forwarded-For`, so the IP limit is bypassable. The per-email limit still held (429 after 3 attempts). |

What to do: set `FORWARDED_ALLOW_IPS` to your platform's proxy address or range (not `*`), then confirm on the real deployment that two
different networks are limited independently. **Not exercised:** your real proxy, its address range, and TLS termination.
Cookies are `HttpOnly; Secure; SameSite=Lax`, so the browser app and the API should share a site (same registrable domain) with HTTPS on both.

## 2. Production configuration

`ENV=production` runs `assert_startup_safe` in both the API and the worker. Exercised: every unsafe combination in
`backend/tests/test_production_config.py` is refused, a safe configuration starts, and `/health/ready` is 503 until the worker has finished
a pass. In production `/docs`, `/redoc` and `/openapi.json` are not served (`tests/test_production_docs_disabled.py`).
**Not exercised:** your real environment values and secrets manager.

## 3. Worker supervision, health checks, alerts

See `docs/MONITORING.md`. Exercised locally: with no worker `/health/ready` is 503; a started worker turns it 200 within seconds; after
`kill -9` it flips to 503 in about 110 seconds (the heartbeat is considered fresh for 120 s) and `/health/alerts` reports
`worker_stopped`; a fresh worker recovers it; a dead webhook event produced exactly one critical POST to an alert receiver, and the
cooldown suppressed repeats.

Consequences: the worker cannot announce its own death, so an **external uptime monitor on `/health/ready` is required**, and the worker must run under a
supervisor that restarts it on any exit (the watchdog exits with code 70 on a stall). **Not exercised:** a real supervisor (systemd, docker
`restart: always`, platform restart policy), a real alert channel, a real uptime monitor.

## 4. Backups and restore

The repo has no backup automation: use your database host's automated backups and point-in-time recovery, and keep your own periodic dump.

Exercised: `pg_dump -Fc` of a populated database, `pg_restore --no-owner` into a fresh database, row counts identical, and Alembic reporting
the same head revision on the restored copy. Do the same drill against your real provider's backup before launch and time it.
**Not exercised:** your host's backup schedule, retention and restore time.

## 5. Migrations: rollback and recovery strategy

* Treat migrations as **forward-only in production**. Take a backup immediately before every `alembic upgrade head`; the recovery path for a bad
  migration is to restore that backup and redeploy the previous release, not to downgrade.
* Downgrades are nevertheless tested: `backend/tests/test_migration_roundtrip.py` runs `upgrade head`, `alembic check`, `downgrade base`,
  and `upgrade head` again on a throwaway database and requires an identical schema. (Two defects in the old downgrade path were fixed:
  a missing index recreation in `e5a7c9d31f46` and enum types left behind by `7c81a009aa17`; neither affects forward upgrades, whose generated
  SQL is byte-identical before and after.)
* A downgrade removes data that exists only in the newer schema. Never run one against production without a backup.

## 6. Still to do on the real deployment

Paddle sandbox run (`docs/PADDLE_SANDBOX_TEST.md`), Retell sandbox run (`docs/RETELL_VERIFICATION.md`), SMTP delivery, error tracking,
a real alert channel, supervisor restart behavior, and a real backup restore. See `docs/LAUNCH_CHECKLIST.md`.


## 7. Request-body limits, trusted proxy and production fail-closed checks (Task 12)

* The application refuses bodies over `MAX_REQUEST_BODY_BYTES` (default 1 MiB) with 413, and over `MAX_WEBHOOK_BODY_BYTES` (default 10 MiB) on `/webhooks/*`
  (Retell sends whole transcripts). It checks the declared length and counts streamed bytes. Also set a limit at the proxy (e.g. nginx `client_max_body_size 10m`).
* Production now **refuses to start** if `FORWARDED_ALLOW_IPS` is `*`, `MONITORING_TOKEN` is under 32 characters, `ALERT_WEBHOOK_URL` or `SENTRY_DSN` is not https
  (in addition to the earlier checks: secret length, https URLs, explicit `PADDLE_ENV`, provider keys). Messages name settings, never values.
* HSTS (2 years, includeSubDomains, preload) and `Secure` session cookies are on whenever `ENV=production`. Verify on the real host with `curl -sI https://…/health`.
* **Still yours to verify on the real host:** TLS certificate and redirect from http, that `FORWARDED_ALLOW_IPS` is your proxy's real address, that two networks are limited independently,
  database TLS (`sslmode=require` if the database is not on a private network) and Redis authentication/private networking. `docker-compose.yml` is for development only.
* Backup/restore: `backend/scripts/backup_restore_drill.sh` automates the drill (dump, restore into a scratch database, compare every table's row count and the Alembic revision, report timings).
  Run it against a copy of your real database. It was exercised here on a freshly migrated, essentially empty schema, not on production-sized data.
