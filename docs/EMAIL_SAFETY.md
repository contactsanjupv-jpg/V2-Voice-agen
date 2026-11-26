# Email safety: automated tests never send real email

## What happened
The full backend suite sent "Your Atla receptionist has been paused" emails to synthetic addresses (`*@credits-test.com`, `*@agent-test.com`)
through the real Resend account in `backend/.env`, exceeding the daily quota.

Cause: `app/core/email.py::send_email` opened SMTP whenever `SMTP_HOST` was set (no test check). Tests that run the real worker pass
(`run_worker.run_once`) drain `service_paused` notification rows left by other tests (`entitlement.py` enqueues them), and `.env` credentials were live.

## Test protection (three layers)
1. **`app/core/email.py`**: `in_test_mode()` is true if `ENV=test`, OR pytest is running (`PYTEST_CURRENT_TEST`), OR conftest forced it.
   In test mode no SMTP connection is opened, whatever credentials are configured. Tests inspect mail with the `outbox` fixture.
2. **`tests/email_safety.py`** (installed by conftest). Exactly what it covers:
   - PROTECTED (tested): `smtplib.SMTP`, `smtplib.SMTP_SSL`, `smtplib.LMTP` and subclasses (to any non-loopback host); `socket.create_connection`,
     `socket.socket.connect` and `connect_ex` to a non-loopback host on a mail port (25, 465, 587, 2465, 2525, 2587).
     An attempt is refused, recorded, and fails the test at teardown even if the code swallowed the error.
   - NOT blocked: connections on other ports (for example an HTTPS email API on 443), DNS lookups, subprocesses, and C-level or third-party
     sockets that bypass Python's `socket` module. Against an HTTPS provider the protection is the inventory test
     (`test_smtplib_is_imported_by_exactly_one_module...`): the only code in `app/` that can send email is `app/core/email.py` (smtplib only),
     and only three modules call it. A new email path fails that test until it is reviewed.
3. **`tests/conftest.py`** blanks `SMTP_HOST/USERNAME/PASSWORD` in the settings object tests see.

Startup also fails closed: `ENV=test` with a non-loopback `SMTP_HOST` is refused (`config.assert_startup_safe`).

## Failure handling
Outcomes: SENT, TRANSIENT (retry with backoff, bounded), PERMANENT (never retried), QUOTA, HELD, NOT_CONFIGURED, BLOCKED (test mode).

- **Rate limiting is not a quota.** 429 / "rate limit" / "too many requests" / "slow down" are TRANSIENT: back off, do not pause all sending.
- **Daily / monthly / unknown quota** (wording: quota, limit, allowance with daily/monthly/per day/per month):
  - reset time stated by the server (timezone-aware ISO time in its message, in the future, at most 45 days): used as is;
  - daily without a stated time: pause until the next UTC midnight + 5 min (at least 30 min). This is a convention, not verified for any provider;
    if the quota is still exhausted the next send pauses again;
  - monthly or unknown without a stated time: **no automatic resume.** An operator runs `python -m app.workers.email_quota clear`. A monthly reset is never guessed.
- Resend's actual SMTP reply text has NOT been verified (no live call is made). Classification is on plain wording, and an unrecognised refusal is
  TRANSIENT (bounded), not an indefinite pause.
- Operator: `python -m app.workers.email_quota status|clear`.

## Where the quota pause lives: PostgreSQL, not Redis
The pause is the newest of two `audit_logs` events (`email.quota_pause`, `email.quota_clear`). It survives Redis outages, restarts and deploys, and every
process (API and worker) reads the same state before each send. Every operator clear is audited. `audit_logs` was chosen over a new table so this patch adds
no migration and cannot create a second Alembic head next to Task 2's. Do not prune `email.quota_*` rows while a pause is meant to be active.

**Order of events.** "Newest" is a sequence number in each event, not a timestamp. Every writer takes one PostgreSQL advisory lock, reads the latest sequence number
and writes +1 in the same transaction, so events are totally ordered by commit order: no ties, no clock skew, and a clear can never land "before" a pause that committed first.
`clear` is compare-and-clear: it clears the pause it looked at, under the lock. If a NEWER pause was recorded meanwhile it is refused (`StalePauseError`, exit 1) and
the newer pause stays. `status` shows the entry number; `clear --seq N` clears only entry N.

**Every send proves the store is writable first.** Reading the breaker is not enough: a database that answers reads but refuses writes (read-only failover, disk
full, lock/statement timeouts) could not record a quota failure, so one sender that reached the provider could not stop the others. So before contacting the provider,
every send does, in ONE transaction under the lock, (1) read the pause and (2) update a single heartbeat row (`email.quota_probe`, updated in place, never grows).
If the read or that write fails, nothing is sent (HELD) and the provider is not contacted. Tested on real PostgreSQL, including a separate OS process whose connection
is read-only (PGOPTIONS).

- If the database refuses the WRITE of a pause after the gate passed (it failed between the gate and the quota reply), the pause is retried 3 times, then held in the
  detecting process and published as soon as the database accepts a write again. Meanwhile every other process is held by the writability gate.
  Residual window (not closable without serialising all sending): the database stops accepting writes after a sender's gate passed, recovers, AND the detecting process
  dies or is idle before it publishes; then the next sender can contact the provider once, meet the quota again, and pause again.
- While the database is down or read-only no email goes out. The API and the outbox are down or degraded then too; notifications keep their attempts and retry every 5 minutes.
- Cost: one short transaction (advisory lock + one UPDATE) per email send.
- **What a clear may discard** (a recorded pause and a newer pause held only in this process's memory can exist together, for example when a send that passed the gate gets its quota reply after another process recorded a pause):
  - `clear --seq N` (qualified) clears ONLY the recorded pause #N the operator inspected. An unrecorded local pause has no entry number, so it is never discarded by a qualified clear; it stays in force and is published to the database by the next check.
  - `clear` (unqualified) is the explicit "resume everything now": it clears the recorded pause and the local pause this process was holding when the clear started. A local pause that appears while the clear is running is newer than anything the operator saw and is kept.
  - If the database cannot be read or written, or the clear is stale, nothing is cleared anywhere and the local pause is kept.
  (The command runs in its own process, so it can only clear what is in the database; a pause held in another process's memory is published to the database by that process.)
- The operator command accepts exactly `status`, `clear`, or `clear --seq N` (N a positive whole number). Anything else exits 2 and changes nothing; an invalid `--seq` is never turned into an unqualified clear.
- Operator: `python -m app.workers.email_quota status|clear [--seq N]` (`clear` writes the audit entry; it says "Nothing was cleared" if the database cannot be reached).

## Notification delivery to several recipients
Each owner/admin is tracked separately and durably in the outbox row (`payload._delivery`: a keyed identifier, `h2:` + truncated HMAC-SHA256 of the address under a subkey derived from `APP_SECRET_KEY`, never the address and not a guessable plain hash). It is committed
right after each accepted send. A retry (after a failure, a quota pause or a crash) only goes to recipients still outstanding. A permanently refused
recipient is not retried and does not block the rest.
(Stored in `payload`, not a new column, so this patch needs no migration and cannot collide with other migration heads.)

## Claiming a notification (two workers, one email)
A worker first claims the row with ONE conditional `UPDATE ... WHERE id=? AND status='pending' AND (next_attempt_at IS NULL OR next_attempt_at <= now)`, which also
moves `next_attempt_at` to now + 10 min and counts the attempt, and commits BEFORE sending. PostgreSQL row-locks the row for the UPDATE; a concurrent claimer waits, re-evaluates the
WHERE against the committed row (READ COMMITTED), no longer matches, and affects 0 rows, so only one worker (rowcount == 1) ever sends. No lock is held during SMTP.
The lease only covers a worker that dies after claiming: after 10 minutes the row is claimed again, and recipients already recorded are not emailed again. The lease is not
the mutual exclusion; the conditional UPDATE is. Tests run two real PostgreSQL connections (see `test_email_safety.py`, "atomic claim").
A worker that is still sending after 10 minutes (it cannot: SMTP timeouts are 10 s per recipient) could be raced by a reclaim.

Remaining limit: a crash between the SMTP hand-off and the commit for ONE recipient can still send that one email twice (at-least-once).

## Production
Unchanged when not in test mode: explicit SMTP settings still deliver. In-app notifications never depend on email success.

## Writing tests
Use the `outbox` fixture (`outbox.sent`, `outbox.to(addr)`, `outbox.subjects()`), never real SMTP.

## The in-memory emergency pause is lock-protected
`_local_pause` is read and written only through four helpers that share one `threading.Lock`: `_get_local`, `_merge_local` (a second pause never weakens the first), `_replace_local` (tests and fixtures) and
`_clear_local_if` (an atomic compare-and-clear: the identity check and the assignment happen in ONE hold of the lock). The lock is never held during database I/O. A newer pause
that arrives at any boundary of a clear (tested at every lock boundary, for qualified and unqualified clears, with and without a recorded pause) is never discarded. Tests fail
if any other production function, test or fixture touches the variable.

## Recipient identifiers and secret rotation
Identifiers recorded for already-emailed recipients are `HMAC(subkey(APP_SECRET_KEY), address)`. If you rotate `APP_SECRET_KEY`, put the old value(s) in `APP_SECRET_KEY_PREVIOUS`
(comma-separated) for as long as notifications recorded under them can still be pending (a day is plenty: rows are retried for at most a few hours), so recipients who already
got a notification are not emailed again. Without it, a rotation causes at most ONE duplicate email per recipient of a notification that was part-delivered at that moment; nothing is lost.
Identifiers written in the unkeyed format of earlier drafts of this feature are still recognised (read-only).

## One merge rule, decided under the database lock
`_compare(a, b)`: a pause with no resume time (an operator must resume it) is stricter than any pause with one; of two with resume times, the later is stricter; equal is a tie.

Every write of a pause goes through `_record_pause`, which runs in ONE transaction: take the advisory lock, read the newest event, and append the pause only if it is STRICTER than the pause
in force at that moment; otherwise write nothing and return the pause that already covers it. The decision is therefore never made from an earlier snapshot, so a stale process cannot publish
over a newer pause or clear, and a weaker pause can never become the newest event over a stricter active one. Two simultaneous reconciliations (even in two processes) record exactly one event.

A local pause is only a write-retry buffer for a database that refused the write. `_reconcile()` records it with `_record_pause` and drops it once the database holds a pause at least as strict
(its own event, or an existing equal/stricter one). If the database cannot be reached, the stricter of the local pause and the last snapshot applies, and it is retried on the next check.
Consequence worth knowing: an operator's unqualified `clear` removes both the recorded pause and the local one it saw; a local pause that was never recorded and appears after the clear is
published afterwards (the fail-safe direction: it can only keep sending stopped).
- a new, weaker quota reply never weakens a stricter pause: the stricter local pause is recorded first, and the weaker reply is then covered by it (no event).
- `send_email_result` therefore reports (`quota_kind`, `retry_after`) the effective pause, so a notification waits for the strictest reset, not the weakest.

## Outbox ownership (fencing) and delivery guarantees
Claiming a notification is one conditional UPDATE that also stamps a random per-claim token into `payload._claim`. Every later write (delivery ledger, final status, quota/held rescheduling,
error handling) re-reads the row `FOR UPDATE` and proceeds only if the token is still its own; otherwise the worker stops and writes nothing (`_LostClaim`). A slow worker whose lease expired
therefore cannot overwrite the new owner's ledger or status.

What is and is not guaranteed:
- Database processing: each claim is exclusive and every write is fenced, so notification state changes exactly once per claim.
- SMTP delivery is AT-LEAST-ONCE, never exactly-once. If the process dies (or the lease expires) after the provider accepted a message and before the ledger commit, that one recipient can receive a second
  copy on retry. Recipients whose acceptance was already committed are never emailed again by normal retries.
- "Accepted by the provider" is not "delivered to an inbox": the provider can still bounce or drop it later, and this code cannot see that.
- Between the breaker check and the provider call a pause can still land (a check-then-send window of milliseconds); the provider's own refusal then re-triggers the pause.
- Resend's SMTP quota wording and reset time are unverified; no live call is made by any test.

## Isolation level (a requirement, enforced)
The lock protocol needs READ COMMITTED: after waiting for the advisory lock, the next statement must take a fresh snapshot. Under REPEATABLE READ or SERIALIZABLE the snapshot is taken by the lock
request itself, before the wait, so the read after it would be stale. Every breaker transaction therefore uses `_breaker_session()`, which sets READ COMMITTED explicitly (whatever the engine default is),
and `_lock()` verifies it right after acquiring the lock and fails closed (the send is HELD) if it is anything else. The lock is the first statement of every transaction that writes or decides.
The only lock-free breaker transaction is the single-statement read that displays a pause; no write is ever decided from it (writes re-read under the lock).
