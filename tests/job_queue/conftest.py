"""
The job queue tests run against a real local Postgres, because the point is how the queries behave.
They're skipped if there's no test database. It gets wiped, so anything not named *_test is refused.
"""

import os

import psycopg
import pytest

from auto_archiver.job_queue import db, migrate

TEST_URL = os.environ.get("AA_TEST_DATABASE_URL", "postgresql://bob:password@localhost:5432/auto_archiver_test")


@pytest.fixture(scope="session")
def schema():
    try:
        conn = db.connect(TEST_URL)
    except psycopg.OperationalError as e:
        pytest.skip(f"No test database: {e}")
    with conn:
        name = conn.execute("SELECT current_database() AS name").fetchone()["name"]
        assert name.endswith("_test"), f"Refusing to wipe {name}"
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        migrate.migrate(conn)


@pytest.fixture(autouse=True)
def test_env_file(tmp_path, monkeypatch):
    """Every test runs in a folder whose .env is the test database, so none can reach a real one"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"AA_DATABASE_URL={TEST_URL}\n")


@pytest.fixture(autouse=True)
def fresh_sync_logs():
    """sync only logs a line when it changes, so each test starts with nothing logged"""
    from auto_archiver.job_queue import sync

    sync._last_logged.clear()


@pytest.fixture
def database_url() -> str:
    return TEST_URL


@pytest.fixture
def conn(schema):
    with db.connect(TEST_URL) as conn:
        conn.execute("TRUNCATE sheets, jobs, outbox, shadow_jobs, alerts, heartbeats RESTART IDENTITY CASCADE")
        yield conn
