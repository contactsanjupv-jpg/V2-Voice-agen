"""Adversarial concurrency tests for the email-safety design. Every test here uses REAL overlapping operations: separate threads
with their own PostgreSQL connections, and separate operating-system processes where process state matters (the local pause
lives in process memory). Nothing here contacts a mail server.

What these tests prove, and what they cannot:
  * the shared pause history is a valid linearisation (no weaker pause over a stricter active one, no clear that names a pause that
    was not the active one, no gaps or duplicates in the order);
  * a claimed notification is owned by exactly one worker at a time and a stale worker writes nothing;
  * SMTP delivery is AT-LEAST-ONCE: a crash after the server accepted a message but before the ledger commit sends that one
    recipient a second copy on retry. That duplicate is asserted here, not hidden."""
from __future__ import annotations

import dataclasses
import os
import pathlib
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest
from app.core import email as email_mod
from app.core.email import EmailOutcome, EmailResult
from app.db.base import SessionLocal
from app.db.models.platform import AuditLog
from app.db.models.tenancy import OrgRole
from app.services import owner_notifications as on
from tests.test_email_safety import _one_paused_row
from tests.test_inbound_call_resolution import _retell_key  # noqa: F401  (autouse: signs the Retell webhook the lead scenario posts)
from tests.test_owner_notifications import (  # noqa: F401  (mailbox: autouse fixture that enables notifications and wipes the outbox)
    LEAD, _add_member, _analyzed, _live_org, _rows, mailbox,
)

BACKEND = pathlib.Path(__file__).resolve().parent.parent
HOUR = timedelta(hours=1)


# ------------------------------------------------------------------ the history validator
def _history():
    db = SessionLocal()
    try:
        rows = db.query(AuditLog).filter(AuditLog.action.in_((email_mod.PAUSE_ACTION, email_mod.CLEAR_ACTION))).all()
        return sorted(((r.audit_metadata["seq"], r.action, r.audit_metadata) for r in rows), key=lambda e: e[0])
    finally:
        db.close()


def _validate_since(before: int, initial):
    """Replays the events written after the first `before` ones and checks every one against the state it was written onto."""
    events = _history()
    new = events[before:]
    prev = events[before - 1][0] if before else 0
    active = initial
    for seq, action, meta in new:
        assert seq == prev + 1, f"order has a gap or a duplicate at #{seq} (after #{prev})"
        prev = seq
        if action == email_mod.PAUSE_ACTION:
            until = datetime.fromisoformat(meta["until"]) if meta["until"] else None
            candidate = email_mod.QuotaPause(meta["kind"], until, meta["source"], seq)
            assert active is None or email_mod._compare(candidate, active) > 0, f"pause #{seq} was written over an equal or stricter active pause #{active.seq}"
            active = candidate
        else:
            assert active is not None and meta["clears_seq"] == active.seq, f"clear #{seq} names #{meta['clears_seq']}, but the active pause was {active}"
            active = None
    final = email_mod._store_read()
    assert (final.seq if final else None) == (active.seq if active else None), "the stored state is not the result of replaying the history"
    return len(new)


def _start():
    return len(_history()), email_mod._store_read()


def _race(*targets):
    """Runs the callables at the same instant; returns their exceptions (None where they finished)."""
    barrier = threading.Barrier(len(targets))
    errors: list = [None] * len(targets)

    def wrap(i, fn):
        def run():
            try:
                barrier.wait(timeout=10)
                fn()
            except BaseException as exc:  # noqa: BLE001
                errors[i] = exc

        return run

    threads = [threading.Thread(target=wrap(i, fn)) for i, fn in enumerate(targets)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert not any(t.is_alive() for t in threads), "a racing operation never finished"
    return errors


# ------------------------------------------------------------------ pause vs pause, pause vs clear
@pytest.mark.parametrize("round_", range(3))
def test_concurrent_pauses_never_write_a_weaker_pause_over_a_stricter_one(round_):
    before, initial = _start()
    now = datetime.now(timezone.utc)
    kinds = [("daily", now + (i + 1) * HOUR) for i in range(6)] + [("monthly", None), ("monthly", None), ("daily", now + 2 * HOUR), ("unknown", None)]
    errors = _race(*[(lambda k=k, r=r: email_mod.pause_for_quota(k, r, now=now)) for k, r in kinds])
    assert errors == [None] * len(kinds)
    written = _validate_since(before, initial)
    assert 1 <= written <= len(kinds)
    assert email_mod._store_read().operator_required  # the strictest pause won, whatever the interleaving
    assert email_mod.check_breaker().pause.operator_required


def test_pauses_and_clears_racing_leave_a_valid_history():
    before, initial = _start()

    def pauser():
        for _ in range(6):
            email_mod.pause_for_quota("monthly")

    def clearer():
        for _ in range(6):
            try:
                email_mod.clear_quota_pause()
            except email_mod.StalePauseError:
                pass  # a pause landed after the clear looked: refused, nothing erased

    errors = _race(pauser, pauser, clearer, clearer)
    assert errors == [None] * 4
    assert _validate_since(before, initial) >= 1


def test_a_qualified_clear_racing_a_newer_pause_never_erases_the_newer_pause():
    for _ in range(5):
        email_mod.clear_quota_pause()
        first = email_mod.pause_for_quota("monthly")
        before, initial = _start()
        outcome = {}

        def qualified_clear():
            try:
                outcome["cleared"] = email_mod.clear_quota_pause(expected_seq=first.seq)
            except email_mod.StalePauseError:
                outcome["cleared"] = "stale"

        def clear_then_pause():
            try:
                email_mod.clear_quota_pause()
            except email_mod.StalePauseError:
                pass  # the qualified clear got in between: refused-or-done, never an overwrite
            email_mod.pause_for_quota("monthly")

        assert _race(qualified_clear, clear_then_pause) == [None, None]
        _validate_since(before, initial)
        final = email_mod._store_read()
        assert final is not None and final.seq > first.seq  # the pause recorded after the operator looked is in force at the end, whatever the order
        assert outcome["cleared"] in (True, "stale")


# ------------------------------------------------------------------ separate processes
_SCRIPT = """
import sys, time
from app.core import email as e
mode, start_at = sys.argv[1], float(sys.argv[2])
if mode == "publish":
    e._replace_local(e.QuotaPause("monthly", None, "operator-required"))
time.sleep(max(0.0, start_at - time.time()))
if mode == "publish":
    state = e.check_breaker(for_sending=True)
    print("seq", state.pause.seq if state.pause else None, "local", e._get_local())
else:
    print("seq", e.pause_for_quota("monthly").seq)
"""


def _spawn(mode, start_at):
    env = {**os.environ, "PYTHONPATH": str(BACKEND)}
    return subprocess.Popen([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, %r); exec(sys.stdin.read() if False else %r)" % (str(BACKEND), _SCRIPT), mode, str(start_at)],
                            cwd=BACKEND, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _finish(procs):
    out = []
    for p in procs:
        stdout, stderr = p.communicate(timeout=120)
        assert p.returncode == 0, stderr[-2000:]
        out.append(stdout.strip())
    return out


def test_two_processes_pausing_at_the_same_instant_record_exactly_one_pause():
    before, initial = _start()
    at = time.time() + 4
    outputs = _finish([_spawn("pause", at), _spawn("pause", at), _spawn("pause", at)])
    assert len({o for o in outputs}) == 1, outputs  # all three processes agree on the pause that is in force
    assert _validate_since(before, initial) == 1


def test_two_processes_reconciling_the_same_local_pause_publish_it_exactly_once():
    before, initial = _start()
    at = time.time() + 4
    outputs = _finish([_spawn("publish", at), _spawn("publish", at)])
    assert all(o.endswith("local None") for o in outputs), outputs  # each dropped its local copy only after the database covered it
    assert len({o.split()[1] for o in outputs}) == 1, outputs
    assert _validate_since(before, initial) == 1


def test_a_clear_racing_another_process_publishing_a_local_pause_leaves_a_valid_history():
    for _ in range(3):
        email_mod.pause_for_quota("daily", datetime.now(timezone.utc) + 3 * HOUR)
        before, initial = _start()
        at = time.time() + 3
        proc = _spawn("publish", at)
        time.sleep(max(0, at - time.time()))
        try:
            email_mod.clear_quota_pause()
        except email_mod.StalePauseError:
            pass
        _finish([proc])
        _validate_since(before, initial)  # the clear and the publication are ordered one way or the other, never interleaved


# ------------------------------------------------------------------ isolation level, lock-wait behaviour, writers, gate
ISOLATION_LEVELS = ["READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"]


@pytest.fixture(params=ISOLATION_LEVELS)
def engine_isolation(request, monkeypatch):
    """Binds the application's session factory to an engine whose DEFAULT isolation level is the parametrised one, to prove the breaker
    does not depend on the engine default."""
    import app.db.base as base
    from app.config import get_settings
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(get_settings().DATABASE_URL, isolation_level=request.param, future=True)
    monkeypatch.setattr(base, "SessionLocal", sessionmaker(bind=engine, autoflush=False, future=True))
    yield request.param
    engine.dispose()


def test_the_application_engine_default_is_read_committed_and_the_breaker_session_is_always_read_committed(engine_isolation):
    from sqlalchemy import text

    from app.db.base import SessionLocal

    plain = SessionLocal()
    try:
        assert plain.execute(text("SELECT current_setting('transaction_isolation')")).scalar() == engine_isolation.lower()
    finally:
        plain.close()
    db = email_mod._breaker_session()
    try:
        assert db.execute(text("SELECT current_setting('transaction_isolation')")).scalar() == "read committed"  # whatever the engine default
    finally:
        db.close()


def test_the_lock_refuses_to_run_in_a_transaction_that_is_not_read_committed():
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings

    engine = create_engine(get_settings().DATABASE_URL, isolation_level="REPEATABLE READ", future=True)
    db = sessionmaker(bind=engine, future=True)()
    try:
        with pytest.raises(RuntimeError, match="READ COMMITTED"):
            email_mod._lock(db)
        db.rollback()
        assert db.execute(text("SELECT 1")).scalar() == 1
    finally:
        db.close()
        engine.dispose()


def test_the_first_statement_of_every_breaker_transaction_that_writes_or_decides_is_the_lock():
    """Statements are grouped into transactions. A transaction that writes the history, or runs more than one statement, starts with the lock.
    The only lock-free transaction is the single-statement read used to show a pause (_store_read); it never decides a write."""
    from sqlalchemy import event

    from app.db.base import engine

    for run in (
        lambda: email_mod._record_pause(email_mod.QuotaPause("monthly", None, "operator-required"), datetime.now(timezone.utc)),
        lambda: email_mod.clear_quota_pause(),
        lambda: email_mod.clear_quota_pause(expected_seq=None),
        lambda: email_mod.check_breaker(for_sending=True),
        lambda: email_mod.pause_for_quota("monthly"),
    ):
        transactions: list[list[str]] = [[]]

        def record(conn, cursor, statement, parameters, context, executemany):
            transactions[-1].append(statement)

        def boundary(conn):
            transactions.append([])

        event.listen(engine, "before_cursor_execute", record)
        event.listen(engine, "commit", boundary)
        event.listen(engine, "rollback", boundary)
        try:
            run()
        finally:
            event.remove(engine, "before_cursor_execute", record)
            event.remove(engine, "commit", boundary)
            event.remove(engine, "rollback", boundary)
        for tx in (t for t in transactions if t):
            touches_history = any("audit_logs" in st for st in tx)
            writes = any(st.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for st in tx)
            if writes or len(tx) > 1:
                assert "pg_advisory_xact_lock" in tx[0], tx
            else:
                assert touches_history and "pg_advisory_xact_lock" not in tx[0] and tx[0].lstrip().upper().startswith("SELECT"), tx  # the read-only display read


def test_a_transaction_that_waits_for_the_lock_sees_what_the_holder_committed(engine_isolation):
    """A holds the lock with an UNCOMMITTED monthly pause. B (a weaker daily pause) blocks on the lock; when A commits, B must read A's pause
    (fresh snapshot) and write nothing. Under REPEATABLE READ / SERIALIZABLE without the explicit READ COMMITTED this would use a stale snapshot."""
    before, initial = _start()
    now = datetime.now(timezone.utc)
    meta = {"kind": "monthly", "until": None, "source": "operator-required", "set_at": now.isoformat()}
    a = email_mod._breaker_session()
    outcome: dict = {}
    try:
        email_mod._append_event(a, email_mod.PAUSE_ACTION, meta)  # A: lock taken, pause added, not yet committed

        def b():
            outcome["pause"] = email_mod._record_pause(email_mod.QuotaPause("daily", now + 2 * HOUR, "server"), now)
            outcome["done"] = True

        thread = threading.Thread(target=b)
        thread.start()
        thread.join(timeout=0.8)
        assert thread.is_alive() and "done" not in outcome  # B is waiting for the lock
        a.commit()
        thread.join(timeout=15)
    finally:
        a.close()
    assert outcome.get("done") and outcome["pause"].operator_required  # B saw A's committed pause and returned it
    assert _validate_since(before, initial) == 1  # exactly one event: B wrote nothing


def test_a_clear_that_waits_for_the_lock_sees_the_pause_committed_while_it_waited(engine_isolation):
    first = email_mod.pause_for_quota("monthly")
    before, initial = _start()
    a = email_mod._breaker_session()
    outcome: dict = {}
    try:
        email_mod._append_event(a, email_mod.CLEAR_ACTION, {"by": "other", "clears_seq": first.seq})  # another operator's clear, uncommitted

        def qualified():
            try:
                outcome["r"] = email_mod.clear_quota_pause(expected_seq=first.seq)
            except email_mod.StalePauseError:
                outcome["r"] = "stale"

        # This clear read #first before the other one committed, then waits at the lock; once the other clear commits it must refuse, not write a second clear.
        thread = threading.Thread(target=qualified)
        thread.start()
        thread.join(timeout=0.8)
        a.commit()
        thread.join(timeout=15)
    finally:
        a.close()
    assert outcome["r"] in ("stale", False)  # nothing left to clear, or the sequence moved: never a second clear over the same pause
    assert _validate_since(before, initial) == 1


@pytest.mark.parametrize("round_", range(2))
def test_concurrent_pauses_stay_valid_under_every_engine_default_isolation(engine_isolation, round_):
    before, initial = _start()
    now = datetime.now(timezone.utc)
    kinds = [("daily", now + (i + 1) * HOUR) for i in range(5)] + [("monthly", None), ("daily", now + 9 * HOUR)]
    assert _race(*[(lambda k=k, r=r: email_mod.pause_for_quota(k, r, now=now)) for k, r in kinds]) == [None] * len(kinds)
    _validate_since(before, initial)
    assert email_mod._store_read().operator_required


def test_every_writer_of_quota_events_goes_through_the_one_locked_protocol():
    """Static inventory. Only core/email.py knows the event names, and inside it only _append_event creates pause/clear rows."""
    import ast

    names = ("email.quota_pause", "email.quota_clear", "PAUSE_ACTION", "CLEAR_ACTION")
    outside = [str(p.relative_to(BACKEND)) for p in (BACKEND / "app").rglob("*.py") if p.name != "email.py" and any(n in p.read_text() for n in names)]
    assert outside == []
    tree = ast.parse((BACKEND / "app/core/email.py").read_text())
    creators = []
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "AuditLog":
                    creators.append(fn.name)
    assert sorted(set(creators)) == ["_append_event", "_gate_store"]  # _gate_store only writes the heartbeat row (PROBE_ACTION), never a pause or clear
    gate = next(fn for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef) and fn.name == "_gate_store")
    assert "PAUSE_ACTION" not in ast.dump(gate) and "CLEAR_ACTION" not in ast.dump(gate)
    callers = [fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef) and any(isinstance(n, ast.Name) and n.id == "_append_event" for n in ast.walk(fn))]
    assert sorted(callers) == ["_store_write"]  # the single caller of the single writer


def test_a_pause_recorded_between_the_gate_and_the_provider_does_not_stop_that_one_send_but_stops_every_later_one(monkeypatch):
    """The documented check-then-send window. The send that already passed the gate goes out; the next send, in any process, is refused without
    contacting the provider."""
    calls: list[str] = []

    def transport(settings, to, subject, body):
        calls.append(to)
        email_mod._record_pause(email_mod.QuotaPause("monthly", None, "operator-required"), datetime.now(timezone.utc))  # another process pauses right now
        return EmailResult(EmailOutcome.SENT)

    monkeypatch.setattr(email_mod, "in_test_mode", lambda: False)
    monkeypatch.setattr(email_mod, "_transport", transport)
    first = email_mod.send_email_result("a@example.test", "s", "b")
    assert first.outcome is EmailOutcome.SENT and calls == ["a@example.test"]  # in flight before the pause: not stopped (cannot be, once past the gate)
    second = email_mod.send_email_result("b@example.test", "s", "b")
    assert second.outcome is EmailOutcome.QUOTA and calls == ["a@example.test"]  # the pause is honoured from the very next send; provider not contacted


# ------------------------------------------------------------------ outbox: crashes, leases, stale workers
_CRASH = """
import os, sys, uuid
from datetime import datetime, timezone
from app.db.base import SessionLocal
from app.services import owner_notifications as on
db = SessionLocal()
token = on._claim(db, uuid.UUID(sys.argv[1]), datetime.fromisoformat(sys.argv[2]))
db.commit()
print("claimed", bool(token), flush=True)
os._exit(9)  # the worker dies holding the claim: no cleanup, nothing written afterwards
"""


def test_a_worker_process_that_dies_holding_a_claim_is_replaced_only_after_the_lease_runs_out():
    org = _one_paused_row("crash")
    nid = _rows(org)[0].id
    now = datetime.now(timezone.utc)
    done = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, %r)\n%s" % (str(BACKEND), _CRASH), str(nid), now.isoformat()],
                          cwd=BACKEND, env={**os.environ, "PYTHONPATH": str(BACKEND)}, capture_output=True, text=True, timeout=120)
    assert done.returncode == 9 and "claimed True" in done.stdout, done.stderr[-1500:]
    db = SessionLocal()
    try:
        assert on._claim(db, nid, now + timedelta(minutes=5)) is None  # the dead worker's lease is still running
        db.rollback()
        assert on._claim(db, nid, now + timedelta(minutes=11)) is not None  # lease expired: another worker takes over
        db.commit()
    finally:
        db.close()
    row = _rows(org)[0]
    assert row.status == "pending" and row.attempts == 2


def _two_recipient_notification():
    org, agent, number = _live_org()
    _add_member(org, OrgRole.admin)
    _analyzed(org, agent, number, LEAD)
    return org, _rows(org)[0].id


def test_a_worker_whose_lease_expired_cannot_overwrite_the_new_owner_and_only_its_in_flight_recipient_is_duplicated(monkeypatch):
    org, nid = _two_recipient_notification()
    now = datetime.now(timezone.utc)
    wire: list[str] = []
    state = {"reentered": False}

    def smtp(to, subject, body):
        wire.append(to)
        if not state["reentered"]:
            state["reentered"] = True  # worker 1 is stuck in this send; meanwhile its lease expires and worker 2 takes over completely
            db2 = SessionLocal()
            try:
                assert on._process(db2, nid, now + timedelta(minutes=11)) == "sent"
            finally:
                db2.close()
        return EmailResult(EmailOutcome.SENT)

    monkeypatch.setattr(on, "send_email_result", smtp)
    db1 = SessionLocal()
    try:
        assert on._process(db1, nid, now) is None  # worker 1 discovers it lost the claim and stops without writing
    finally:
        db1.close()
    row = _rows(org)[0]
    assert row.status == "sent" and row.attempts == 2
    assert len(row.payload["_delivery"]["sent"]) == 2 and row.payload["_delivery"]["refused"] == []  # worker 1's stale copy never replaced worker 2's ledger
    counts = Counter(wire)
    assert len(counts) == 2 and sorted(counts.values()) == [1, 2] and len(wire) == 3  # exactly one duplicate: the recipient in flight when the lease expired


def test_a_stale_worker_cannot_write_into_a_row_the_new_owner_still_holds_pending(monkeypatch):
    """The new owner has NOT finished (its sends failed, so the row is pending again): only the per-claim token tells the old worker it is stale."""
    org, nid = _two_recipient_notification()
    now = datetime.now(timezone.utc)
    state = {"worker": 1}

    def smtp(to, subject, body):
        if state["worker"] == 2:
            return EmailResult(EmailOutcome.TRANSIENT, "try later")
        state["worker"] = 2  # worker 1 is stuck in this send; its lease expires and worker 2 takes over but gets only refusals
        db2 = SessionLocal()
        try:
            assert on._process(db2, nid, now + timedelta(minutes=11)) == "pending"
        finally:
            db2.close()
        return EmailResult(EmailOutcome.SENT)  # worker 1's send is accepted afterwards

    monkeypatch.setattr(on, "send_email_result", smtp)
    db1 = SessionLocal()
    try:
        assert on._process(db1, nid, now) is None
    finally:
        db1.close()
    row = _rows(org)[0]
    assert row.status == "pending" and row.attempts == 2
    assert row.payload.get("_delivery", {}).get("sent", []) == []  # worker 1's stale "sent" record was refused


@pytest.mark.parametrize("final", ["sent", "failed", "skipped"])
def test_a_finished_notification_can_never_be_claimed_again(final):
    org = _one_paused_row("fin")
    nid = _rows(org)[0].id
    db = SessionLocal()
    try:
        row = db.get(on.OwnerNotification, nid)
        row.status, row.next_attempt_at = final, None
        db.commit()
        assert on._claim(db, nid, datetime.now(timezone.utc) + timedelta(days=30)) is None
        db.rollback()
    finally:
        db.close()


class _Crash(BaseException):
    """Stands in for the process dying (SIGKILL, OOM, power loss): not an Exception, so nothing in the worker may swallow it."""


def test_a_crash_after_the_server_accepted_a_message_duplicates_only_that_recipient_on_retry(monkeypatch):
    org, nid = _two_recipient_notification()
    now = datetime.now(timezone.utc)
    wire: list[str] = []

    def smtp_then_die(to, subject, body):
        wire.append(to)  # the provider ACCEPTED this message...
        if len(wire) == 2:
            raise _Crash()  # ...and the process died before the delivery ledger was committed
        return EmailResult(EmailOutcome.SENT)

    monkeypatch.setattr(on, "send_email_result", smtp_then_die)
    db = SessionLocal()
    try:
        with pytest.raises(_Crash):
            on._process(db, nid, now)
    finally:
        db.close()
    row = _rows(org)[0]
    assert row.status == "pending" and len(row.payload["_delivery"]["sent"]) == 1  # the first recipient's acceptance was committed before the crash

    monkeypatch.setattr(on, "send_email_result", lambda to, s, b: (wire.append(to), EmailResult(EmailOutcome.SENT))[1])
    db = SessionLocal()
    try:
        assert on._process(db, nid, now + timedelta(minutes=11)) == "sent"
    finally:
        db.close()
    counts = Counter(wire)
    assert len(wire) == 3 and sorted(counts.values()) == [1, 2]  # at-least-once: the in-flight recipient got a second copy; the committed one did not
    assert len(_rows(org)[0].payload["_delivery"]["sent"]) == 2


def test_two_workers_claiming_the_same_notification_at_once_send_it_exactly_once_each_recipient(monkeypatch):
    org, nid = _two_recipient_notification()
    now = datetime.now(timezone.utc)
    wire: list[str] = []
    lock = threading.Lock()

    def smtp(to, s, b):
        with lock:
            wire.append(to)
        return EmailResult(EmailOutcome.SENT)

    monkeypatch.setattr(on, "send_email_result", smtp)
    results: list = []

    def worker():
        db = SessionLocal()
        try:
            results.append(on._process(db, nid, now))
        finally:
            db.close()

    assert _race(worker, worker, worker, worker) == [None] * 4
    assert sorted(results, key=str) == [None, None, None, "sent"]  # one winner; the rest saw a claimed row and did nothing
    assert sorted(Counter(wire).values()) == [1, 1] and len(wire) == 2


def test_nothing_in_the_code_base_deletes_audit_log_rows():
    """The pause history is a log in audit_logs. If anything pruned it, an expected-sequence check could be fooled."""
    import re

    pattern = re.compile(r"query\(\s*AuditLog\s*\)[^\n]*\.delete\(|delete\(\s*AuditLog\s*\)|DELETE\s+FROM\s+audit_logs|TRUNCATE[^\n]*audit_logs", re.I)
    offenders = [str(p.relative_to(BACKEND)) for p in (BACKEND / "app").rglob("*.py") if pattern.search(p.read_text())]
    offenders += [str(p.relative_to(BACKEND)) for p in (BACKEND / "alembic").rglob("*.py") if pattern.search(p.read_text())]
    assert offenders == []


_ = (dataclasses, uuid)
