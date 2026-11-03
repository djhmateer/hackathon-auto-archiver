"""Tests for the job queue's SQL. The fixtures are in conftest.py"""

import threading
from uuid import uuid4

import psycopg
import pytest

from auto_archiver.job_queue import db


def add_sheet(conn, gid=1, weight=1, enabled=True) -> int:
    return conn.execute(
        """
        INSERT INTO sheets (spreadsheet_id, worksheet_gid, worksheet, config, weight, enabled)
        VALUES ('spreadsheet1', %s, 'tab1', 'secrets/orchestration-example.yaml', %s, %s) RETURNING id
        """,
        (gid, weight, enabled),
    ).fetchone()["id"]


def add_job(conn, sheet_id, row=2, domain=None) -> int | None:
    """Each row gets its own domain unless one is given, as only one job per domain runs at a time"""
    job_id = db.add_job(conn, sheet_id, row, f"https://example.com/post/{row}", domain or f"example{row}.com")
    if job_id is not None:
        make_available(conn, job_id)
    return job_id


def job(conn, job_id) -> dict:
    return conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()


def sheet_writes(conn, job_id) -> list[dict]:
    return conn.execute("SELECT * FROM sheet_writes WHERE job_id = %s ORDER BY id", (job_id,)).fetchall()


def expire_lease(conn, job_id, ago="3 minutes") -> None:
    conn.execute("UPDATE jobs SET lease_until = now() - %s::interval WHERE id = %s", (ago, job_id))


def make_available(conn, job_id) -> None:
    # in the past, as the clock on WSL2 can step back ~150 ms, which makes a job just added look not yet available
    conn.execute("UPDATE jobs SET available_at = now() - interval '1 minute' WHERE id = %s", (job_id,))


# adding jobs


def test_add_job_queues_the_status_in_the_same_step(conn):
    job_id = add_job(conn, add_sheet(conn))
    entries = sheet_writes(conn, job_id)
    assert job(conn, job_id)["status"] == "queued"
    assert [(e["cells"], e["expected_status"]) for e in entries] == [({"status": "Queued"}, "")]


def test_row_can_only_have_one_active_job(conn):
    sheet = add_sheet(conn)
    first = add_job(conn, sheet)
    assert add_job(conn, sheet) is None
    assert len(sheet_writes(conn, first)) == 1

    claimed = db.claim_job(conn, "vm1")
    assert add_job(conn, sheet) is None, "a running job is active too"

    db.finish_job(conn, first, claimed["lease_token"], "done", {"status": "ok"})
    assert add_job(conn, sheet) is not None, "a finished row can be archived again"


def test_same_row_on_another_sheet_is_a_different_job(conn):
    assert add_job(conn, add_sheet(conn, gid=1)) is not None
    assert add_job(conn, add_sheet(conn, gid=2)) is not None


def test_weight_must_be_positive(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        add_sheet(conn, weight=0)


# claiming


def test_claim_returns_none_with_nothing_to_do(conn):
    assert db.claim_job(conn, "vm1") is None


def test_claim_sets_the_lease_and_returns_the_config(conn):
    job_id = add_job(conn, add_sheet(conn))
    claimed = db.claim_job(conn, "vm1")
    assert claimed["id"] == job_id
    assert claimed["status"] == "running"
    assert claimed["worker"] == "vm1"
    assert claimed["attempts"] == 1
    assert claimed["config"] == "secrets/orchestration-example.yaml"
    assert claimed["lease_token"] is not None
    assert claimed["started_at"] is not None


def test_claim_adds_the_in_progress_status(conn):
    job_id = add_job(conn, add_sheet(conn))
    db.claim_job(conn, "vm1")
    entry = sheet_writes(conn, job_id)[-1]
    assert entry["cells"] == {"status": "Archive in progress (vm1)"}
    assert entry["expected_status"] == "Queued"


def test_claim_skips_disabled_sheets(conn):
    add_job(conn, add_sheet(conn, enabled=False))
    assert db.claim_job(conn, "vm1") is None


def test_claim_skips_jobs_not_yet_available(conn):
    job_id = add_job(conn, add_sheet(conn))
    conn.execute("UPDATE jobs SET available_at = now() + interval '1 minute' WHERE id = %s", (job_id,))
    assert db.claim_job(conn, "vm1") is None


def test_claim_skips_paused_sheets_until_the_pause_ends(conn):
    sheet = add_sheet(conn)
    add_job(conn, sheet)
    conn.execute("UPDATE sheets SET paused_until = now() + interval '1 minute' WHERE id = %s", (sheet,))
    assert db.claim_job(conn, "vm1") is None
    conn.execute("UPDATE sheets SET paused_until = now() - interval '1 minute' WHERE id = %s", (sheet,))
    assert db.claim_job(conn, "vm1") is not None


def test_oldest_job_goes_first(conn):
    sheet = add_sheet(conn)
    first, second = add_job(conn, sheet, row=2), add_job(conn, sheet, row=3)
    assert db.claim_job(conn, "vm1")["id"] == first
    assert db.claim_job(conn, "vm1")["id"] == second


def test_sheet_with_no_running_jobs_goes_ahead_of_a_backlog(conn):
    busy, quiet = add_sheet(conn, gid=1), add_sheet(conn, gid=2)
    busy_jobs = [add_job(conn, busy, row=row) for row in range(2, 5)]
    quiet_job = add_job(conn, quiet, domain="quiet.example")
    assert db.claim_job(conn, "vm1")["id"] == busy_jobs[0]
    assert db.claim_job(conn, "vm1")["id"] == quiet_job


def test_weight_gives_a_sheet_a_bigger_share(conn):
    heavy, light = add_sheet(conn, gid=1, weight=2), add_sheet(conn, gid=2, weight=1)
    heavy_jobs = [add_job(conn, heavy, row=row) for row in range(2, 5)]
    light_job = add_job(conn, light, domain="light.example")
    assert db.claim_job(conn, "vm1")["id"] == heavy_jobs[0]
    # heavy is at 1/2 and light at 0/1, so light goes next
    assert db.claim_job(conn, "vm1")["id"] == light_job
    # heavy is at 1/2 with nothing left on light
    assert db.claim_job(conn, "vm1")["id"] == heavy_jobs[1]


def test_only_one_job_per_domain_runs_at_a_time(conn):
    sheet = add_sheet(conn)
    first = add_job(conn, sheet, row=2, domain="vimeo.com")
    second = add_job(conn, sheet, row=3, domain="vimeo.com")
    other = add_job(conn, sheet, row=4, domain="youtube.com")
    assert db.claim_job(conn, "vm1")["id"] == first
    assert db.claim_job(conn, "vm2")["id"] == other, "skips the second vimeo.com job"
    assert db.claim_job(conn, "vm3") is None

    token = job(conn, first)["lease_token"]
    db.finish_job(conn, first, token, "done", {"status": "ok"})
    assert db.claim_job(conn, "vm3")["id"] == second


def test_workers_claiming_at_once_never_run_two_jobs_for_one_domain(conn, database_url):
    sheet = add_sheet(conn)
    for row in range(2, 12):
        add_job(conn, sheet, row=row, domain="vimeo.com")
    workers = 8
    start = threading.Barrier(workers)
    claimed, errors = [], []

    def claim_once(name):
        try:
            with db.connect(database_url) as worker_conn:
                start.wait()
                if found := db.claim_job(worker_conn, name):
                    claimed.append(found["id"])
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=claim_once, args=(f"vm{i}",)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(claimed) == 1


def test_workers_claiming_at_once_never_get_the_same_job(conn, database_url):
    sheet = add_sheet(conn)
    job_ids = {add_job(conn, sheet, row=row) for row in range(2, 42)}
    workers = 8
    start = threading.Barrier(workers)
    claimed = []
    errors = []

    def claim_until_empty(name):
        try:
            with db.connect(database_url) as worker_conn:
                start.wait()
                while found := db.claim_job(worker_conn, name):
                    claimed.append(found["id"])
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=claim_until_empty, args=(f"vm{i}",)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sorted(claimed) == sorted(job_ids)
    for job_id in job_ids:
        assert len(sheet_writes(conn, job_id)) == 2, "one Queued and one in progress entry"


# leases


def test_renew_extends_the_lease(conn):
    job_id = add_job(conn, add_sheet(conn))
    claimed = db.claim_job(conn, "vm1")
    expire_lease(conn, job_id, ago="1 minute")
    assert db.renew_lease(conn, job_id, claimed["lease_token"]) is True
    still_valid = conn.execute("SELECT lease_until > now() AS ok FROM jobs WHERE id = %s", (job_id,)).fetchone()
    assert still_valid["ok"]


def test_renew_accepts_the_token_as_a_string(conn):
    job_id = add_job(conn, add_sheet(conn))
    claimed = db.claim_job(conn, "vm1")
    assert db.renew_lease(conn, job_id, str(claimed["lease_token"])) is True


def test_renew_fails_with_the_wrong_token(conn):
    job_id = add_job(conn, add_sheet(conn))
    db.claim_job(conn, "vm1")
    assert db.renew_lease(conn, job_id, uuid4()) is False


def test_replaced_worker_cannot_renew_or_save(conn):
    job_id = add_job(conn, add_sheet(conn))
    old = db.claim_job(conn, "vm1")
    expire_lease(conn, job_id)
    db.requeue_expired(conn)
    make_available(conn, job_id)
    new = db.claim_job(conn, "vm2")
    entries_before = len(sheet_writes(conn, job_id))

    assert db.renew_lease(conn, job_id, old["lease_token"]) is False
    assert db.finish_job(conn, job_id, old["lease_token"], "done", {"status": "old"}) is False
    assert db.release_setup_failure(conn, job_id, old["lease_token"]) is False
    assert db.mark_work_started(conn, job_id, old["lease_token"]) is False

    now = job(conn, job_id)
    assert now["status"] == "running"
    assert now["worker"] == "vm2"
    assert now["lease_token"] == new["lease_token"]
    assert now["work_started_at"] is None
    assert len(sheet_writes(conn, job_id)) == entries_before


# finishing


def test_finish_saves_the_job_and_its_sheet_update_together(conn):
    job_id = add_job(conn, add_sheet(conn))
    claimed = db.claim_job(conn, "vm1")
    assert db.finish_job(conn, job_id, claimed["lease_token"], "done", {"status": "ok", "title": "A title"}) is True

    finished = job(conn, job_id)
    assert finished["status"] == "done"
    assert finished["finished_at"] is not None
    entry = sheet_writes(conn, job_id)[-1]
    assert entry["cells"] == {"status": "ok", "title": "A title"}
    assert entry["expected_status"] == "Archive in progress"


def test_finish_can_record_a_failure(conn):
    job_id = add_job(conn, add_sheet(conn))
    claimed = db.claim_job(conn, "vm1")
    assert db.finish_job(conn, job_id, claimed["lease_token"], "failed", {"status": "failed"}, error="killed") is True
    assert job(conn, job_id)["status"] == "failed"
    assert job(conn, job_id)["error"] == "killed"


def test_finish_only_counts_once(conn):
    job_id = add_job(conn, add_sheet(conn))
    token = db.claim_job(conn, "vm1")["lease_token"]
    assert db.finish_job(conn, job_id, token, "done", {"status": "ok"}) is True
    assert db.finish_job(conn, job_id, token, "failed", {"status": "failed"}) is False
    assert job(conn, job_id)["status"] == "done"


def test_finish_rolls_back_the_job_if_the_sheet_update_fails(conn):
    job_id = add_job(conn, add_sheet(conn))
    token = db.claim_job(conn, "vm1")["lease_token"]
    entries_before = len(sheet_writes(conn, job_id))
    with pytest.raises(TypeError):
        db.finish_job(conn, job_id, token, "done", {"not json": object()})
    assert job(conn, job_id)["status"] == "running"
    assert len(sheet_writes(conn, job_id)) == entries_before


# cleanup


def test_cleanup_leaves_live_leases_alone(conn):
    job_id = add_job(conn, add_sheet(conn))
    db.claim_job(conn, "vm1")
    assert db.requeue_expired(conn) == (0, 0)
    assert job(conn, job_id)["status"] == "running"


def test_cleanup_waits_for_the_grace_period(conn):
    job_id = add_job(conn, add_sheet(conn))
    db.claim_job(conn, "vm1")
    expire_lease(conn, job_id, ago="1 minute")
    assert db.requeue_expired(conn) == (0, 0)
    assert job(conn, job_id)["status"] == "running"


def test_cleanup_requeues_with_a_delay(conn):
    job_id = add_job(conn, add_sheet(conn))
    db.claim_job(conn, "vm1")
    expire_lease(conn, job_id)
    assert db.requeue_expired(conn) == (1, 0)

    requeued = job(conn, job_id)
    assert requeued["status"] == "queued"
    assert requeued["lease_token"] is None
    assert requeued["worker"] is None
    assert requeued["attempts"] == 1
    assert db.claim_job(conn, "vm2") is None, "still in its delay"


def test_retry_keeps_the_first_start_time_and_adds_no_status(conn):
    job_id = add_job(conn, add_sheet(conn))
    first = db.claim_job(conn, "vm1")
    db.mark_work_started(conn, job_id, first["lease_token"])
    expire_lease(conn, job_id)
    db.requeue_expired(conn)
    make_available(conn, job_id)
    entries_before = len(sheet_writes(conn, job_id))

    second = db.claim_job(conn, "vm2")
    assert second["attempts"] == 2
    assert second["started_at"] == first["started_at"]
    assert second["work_started_at"] is None
    assert second["lease_token"] != first["lease_token"]
    assert len(sheet_writes(conn, job_id)) == entries_before


def test_cleanup_fails_the_job_after_three_attempts(conn):
    job_id = add_job(conn, add_sheet(conn))
    for attempt in range(1, 4):
        make_available(conn, job_id)
        assert db.claim_job(conn, "vm1")["attempts"] == attempt
        expire_lease(conn, job_id)
        assert db.requeue_expired(conn) == ((0, 1) if attempt == 3 else (1, 0))

    failed = job(conn, job_id)
    assert failed["status"] == "failed"
    assert failed["finished_at"] is not None
    entry = sheet_writes(conn, job_id)[-1]
    assert entry["cells"]["status"] == "Archive failed - lost its worker 3 times - please rerun"
    assert entry["expected_status"] == "Archive in progress"


# setup failures


def test_setup_failure_gives_back_the_attempt_with_a_delay(conn):
    job_id = add_job(conn, add_sheet(conn))
    token = db.claim_job(conn, "vm1")["lease_token"]
    assert db.release_setup_failure(conn, job_id, token) is True

    released = job(conn, job_id)
    assert released["status"] == "queued"
    assert released["attempts"] == 0
    assert released["lease_token"] is None
    assert db.claim_job(conn, "vm1") is None, "still in its delay"


def test_reclaim_after_setup_failure_adds_no_second_in_progress(conn):
    job_id = add_job(conn, add_sheet(conn))
    token = db.claim_job(conn, "vm1")["lease_token"]
    db.release_setup_failure(conn, job_id, token)
    make_available(conn, job_id)
    assert db.claim_job(conn, "vm1")["attempts"] == 1
    statuses = [e["cells"]["status"] for e in sheet_writes(conn, job_id)]
    assert statuses == ["Queued", "Archive in progress (vm1)"]


def test_setup_failure_is_refused_once_work_has_started(conn):
    job_id = add_job(conn, add_sheet(conn))
    token = db.claim_job(conn, "vm1")["lease_token"]
    assert db.mark_work_started(conn, job_id, token) is True
    assert db.release_setup_failure(conn, job_id, token) is False
    assert job(conn, job_id)["status"] == "running"


def fail_setup(conn, job_id):
    make_available(conn, job_id)
    conn.execute("UPDATE sheets SET paused_until = NULL")
    token = db.claim_job(conn, "vm1")["lease_token"]
    db.release_setup_failure(conn, job_id, token)


def alerts(conn) -> list[dict]:
    return conn.execute("SELECT * FROM admin_alerts").fetchall()


def sheet_row(conn, sheet_id) -> dict:
    return conn.execute("SELECT * FROM sheets WHERE id = %s", (sheet_id,)).fetchone()


def test_sheet_pauses_at_three_setup_failures_with_one_alert(conn):
    sheet = add_sheet(conn)
    job_id = add_job(conn, sheet)

    fail_setup(conn, job_id)
    fail_setup(conn, job_id)
    assert sheet_row(conn, sheet)["paused_until"] is None
    assert alerts(conn) == []

    fail_setup(conn, job_id)
    assert sheet_row(conn, sheet)["paused_until"] is not None
    assert [a["kind"] for a in alerts(conn)] == ["sheet_paused"]

    # still broken: paused again by each failure, with no more alerts
    fail_setup(conn, job_id)
    assert sheet_row(conn, sheet)["paused_until"] is not None
    assert len(alerts(conn)) == 1


def test_paused_sheet_does_not_stop_other_sheets(conn):
    broken, healthy = add_sheet(conn, gid=1), add_sheet(conn, gid=2)
    job_id = add_job(conn, broken)
    for _ in range(3):
        fail_setup(conn, job_id)
    make_available(conn, job_id)
    healthy_job = add_job(conn, healthy)
    assert db.claim_job(conn, "vm1")["id"] == healthy_job


def test_getting_past_setup_clears_the_failures(conn):
    sheet = add_sheet(conn)
    job_id = add_job(conn, sheet)
    for _ in range(3):
        fail_setup(conn, job_id)

    make_available(conn, job_id)
    conn.execute("UPDATE sheets SET paused_until = NULL")
    token = db.claim_job(conn, "vm1")["lease_token"]
    assert db.mark_work_started(conn, job_id, token) is True

    assert job(conn, job_id)["work_started_at"] is not None
    row = sheet_row(conn, sheet)
    assert row["setup_failures"] == 0
    assert row["paused_until"] is None


# the connection string


def test_database_url_comes_from_the_env_file(tmp_path):
    env_file = tmp_path / "some.env"
    env_file.write_text('OTHER=1\nAA_DATABASE_URL="postgresql://user:pass@host:5432/db?a=b"\n')
    assert db.database_url(env_file) == "postgresql://user:pass@host:5432/db?a=b"


def test_an_environment_variable_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("AA_DATABASE_URL", "postgresql://from-environment")
    env_file = tmp_path / "some.env"
    env_file.write_text("AA_DATABASE_URL=postgresql://from-file\n")
    assert db.database_url(env_file) == "postgresql://from-file"


def test_no_env_file_says_what_to_do(tmp_path):
    with pytest.raises(RuntimeError, match="add AA_DATABASE_URL"):
        db.database_url(tmp_path / "missing.env")


def test_describe_database_leaves_out_the_password():
    described = db.describe_database("postgresql://user1:secret@203.0.113.7:5433/db1")
    assert described == "user1@203.0.113.7:5433/db1"


def test_claim_saves_the_workers_git_commit(conn):
    add_job(conn, add_sheet(conn))
    assert db.claim_job(conn, "vm1", "abc1234")["git_commit"] == "abc1234"


def test_a_retry_keeps_each_attempts_files(conn):
    job_id = add_job(conn, add_sheet(conn))
    files = [{"role": "", "key": "folder1/video.mp4", "bytes": 1000}]
    for _ in range(2):
        claimed = db.claim_job(conn, "vm1")
        db.finish_job(conn, job_id, claimed["lease_token"], "done", {"status": "ok"}, files=files)
        conn.execute("UPDATE jobs SET status = 'queued' WHERE id = %s", (job_id,))
    rows = conn.execute("SELECT attempt, bytes FROM job_files WHERE job_id = %s ORDER BY id", (job_id,)).fetchall()
    assert [(r["attempt"], r["bytes"]) for r in rows] == [(1, 1000), (2, 1000)]
