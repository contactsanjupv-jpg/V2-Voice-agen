"""
The Alembic chain must work from an EMPTY database in both directions: `upgrade head` builds exactly the schema the
models describe, `downgrade base` removes all of it (a broken initial downgrade left 24 tables behind), and upgrading again
lands on the identical schema. Runs against a throwaway database created and dropped by the test, never the shared test DB.
"""
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.config import get_settings

BACKEND = Path(__file__).resolve().parents[1]


def _alembic(url: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": url}
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=300)


def _schema(engine) -> dict:
    with engine.connect() as c:
        return {
            "columns": c.execute(text(
                "SELECT table_name, column_name, data_type, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_schema='public' ORDER BY 1, 2")).fetchall(),
            "indexes": c.execute(text("SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname='public' ORDER BY 1, 2")).fetchall(),
            "constraints": c.execute(text(
                "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE connamespace = 'public'::regnamespace ORDER BY 1, 2")).fetchall(),
            "enums": c.execute(text("SELECT t.typname, e.enumlabel FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid ORDER BY 1, e.enumsortorder")).fetchall(),
        }


@pytest.fixture
def scratch_database():
    base = make_url(get_settings().DATABASE_URL)
    name = f"atla_test_migrations_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name).render_as_string(hide_password=False)
    engine = create_engine(url)
    try:
        yield url, engine
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_upgrade_downgrade_upgrade_from_an_empty_database(scratch_database):
    url, engine = scratch_database

    up = _alembic(url, "upgrade", "head")
    assert up.returncode == 0, up.stderr[-2000:]
    check = _alembic(url, "check")  # the migrations describe exactly what the models describe
    assert check.returncode == 0, check.stdout[-1500:] + check.stderr[-1500:]
    first = _schema(engine)
    assert len(first["columns"]) > 100

    down = _alembic(url, "downgrade", "base")
    assert down.returncode == 0, down.stderr[-2500:]
    with engine.connect() as c:
        tables = {r[0] for r in c.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'"))}
        enums = c.execute(text("SELECT count(*) FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid")).scalar()
    assert tables <= {"alembic_version"}, f"downgrade base left tables behind: {sorted(tables)}"
    assert enums == 0, "downgrade base left enum types behind"

    again = _alembic(url, "upgrade", "head")
    assert again.returncode == 0, again.stderr[-2000:]
    assert _schema(engine) == first  # the round trip lands on the identical schema


def test_there_is_exactly_one_migration_head():
    heads = subprocess.run([sys.executable, "-m", "alembic", "heads"], cwd=BACKEND, capture_output=True, text=True, timeout=120)
    assert heads.returncode == 0, heads.stderr[-1000:]
    assert heads.stdout.count("(head)") == 1, heads.stdout
