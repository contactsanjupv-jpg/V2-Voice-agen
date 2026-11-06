# Granting platform admin in a SANDBOX (one-off)

Platform admin is a row in `admin_users`; `require_platform_admin` checks only that table (`users.is_platform_admin` is an unused column and is
never touched). The only route behind it today is `POST /api/v1/admin/refunds`, which the Paddle sandbox runbook (`PADDLE_SANDBOX_TEST.md`, step 6) needs.

`python -m app.workers.grant_sandbox_platform_admin` is the only supported way to create that row. It is a **sandbox pilot tool, not an
admin-management system**: it has no HTTP route (nothing under `app/api` or `app/main.py` references it), it can only add, never remove, and it
refuses to run unless every check below passes. A refusal exits with code 2 and changes nothing.

| Check | Refuses when |
|---|---|
| Environment allowlist | `ENV` is anything but `development` or `test` (so `production` and any unknown value are refused), or `PADDLE_ENV` is not `sandbox` |
| Database driver (the scheme of `DATABASE_URL`) | it is not exactly `postgresql`, `postgresql+psycopg` or `postgresql+psycopg2` (case-sensitive). Every other driver is refused, e.g. `postgresql+asyncpg`, `postgresql+pg8000`, `postgres`; their connection settings were not reviewed. The test-suite guard applies the same rule |
| Database connection routing (from `DATABASE_URL` and the `PG*` environment, no connection needed) | the URL could reach another machine. Fail-closed shared check (`app/db/url_safety.py`, also used by the test-suite guard): the host must be `localhost`, `127.0.0.1`, `::1`, a standard socket directory (`/var/run/postgresql`, `/run/postgresql`, `/tmp`, `/var/tmp`) or absent; refused are `?host=` beside or instead of a URL host, repeated or comma-separated hosts, a non-loopback `hostaddr`, `service`/`port`/`dbname`/`user`/`options` and any other unrecognised query parameter, and `PGHOST`, `PGHOSTADDR` or `PGSERVICE` pointing elsewhere |
| Database name | it does not match `<words joined by _>_test` or `<words joined by _>_sandbox`: the LAST word must be exactly `test` or `sandbox`. `atla_test` and `atla_sandbox` pass; `atla_latest`, `atla_contest`, `atla_testing`, `atla_tests`, `atla_test_prod` and `atla` are refused |
| `--confirm-database` | missing, or not exactly the database name from `DATABASE_URL` (one factor of several: it can never override the checks above) |
| `--email` | no user, or more than one user, matches (matched case-insensitively) |
| `--confirm-user-id` | missing (the tool then only prints the id), malformed, or not that user's id |

Exit codes:

| Code | Meaning | What to do |
|---|---|---|
| `0` | granted, or already an admin | nothing |
| `2` | refused by a safety check; nothing changed | fix the cause named in the message |
| `1` | failed **before** the commit; nothing was committed, so this run granted nothing | read the log, fix, re-run |
| `3` | the commit **succeeded** but the check afterwards failed. This is not a rollback: the grant is most likely in place | **do not re-run blindly**; run the two `SELECT`s the tool prints against `admin_users` and `audit_logs` |
| `4` | the commit itself did not confirm; the outcome is unknown | **do not re-run blindly**; run the same two `SELECT`s first |

On success it writes the `admin_users` row and one `audit_logs` row (`platform_admin.granted`, target `admin_user`, metadata: user id, database,
environment, `via`) in a single transaction. Running it again for an existing admin changes nothing and writes no second audit row.

## Procedure

Use a user you created yourself in the sandbox; the account must already exist (sign up normally first). From `backend/`, in the sandbox environment:

```bash
# 1. Look: finds the user and prints its id. Changes nothing (exit code 2 by design).
python -m app.workers.grant_sandbox_platform_admin --email you@example.com --confirm-database <database name from DATABASE_URL>

# 2. Check the printed id is the account you mean, then grant:
python -m app.workers.grant_sandbox_platform_admin --email you@example.com --confirm-database <database name from DATABASE_URL> \
    --confirm-user-id <id printed in step 1>
```

Admin rights apply immediately to that user's existing session (the check is a live lookup on every request); no new login is needed.

## Verify without moving money

While signed in as that user, send `POST /api/v1/admin/refunds` with an **invalid** body such as `{}`: an admin gets a validation error (422); a
non-admin gets 403. Do not send a valid refund body until Paddle sandbox step 6.

## Revoke after testing

There is deliberately no revoke command. In the sandbox database: `DELETE FROM admin_users WHERE user_id = '<id>';` (takes effect immediately).

## Not for production

Never run this against a real database: it refuses production by configuration, but the safeguard is only as good as the environment you
point it at. A production admin needs a reviewed process of its own (see `DEPLOYMENT.md`; consider MFA and a dedicated admin account).

The test suite has its own, stricter guard (`backend/tests/db_safety.py`): it drops every table at the end of a run, so it accepts only a local database whose name ends in exactly `_test`.
