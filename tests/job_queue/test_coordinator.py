"""The coordinator runs its steps in turn, and one failing doesn't stop the others"""

import psycopg
import pytest

from auto_archiver.job_queue import coordinator, db


def test_one_failing_step_does_not_stop_the_others(conn, mocker):
    sync = mocker.patch("auto_archiver.job_queue.sync.sync_cycle", side_effect=RuntimeError("a Sheets outage"))
    writer = mocker.patch("auto_archiver.job_queue.writer.writer_cycle", side_effect=RuntimeError("a Sheets outage"))
    cleanup = mocker.patch("auto_archiver.job_queue.db.requeue_expired", return_value=(0, 0))
    coordinator.cycle(conn, sync_due=True)
    sync.assert_called_once()
    writer.assert_called_once()
    cleanup.assert_called_once()
    assert conn.execute("SELECT count(*) AS n FROM heartbeats WHERE name = 'coordinator'").fetchone()["n"] == 1


def test_startup_logs_the_tabs_and_queue_but_not_the_password(conn, caplog):
    sheet = conn.execute(
        """
        INSERT INTO sheets (spreadsheet_id, worksheet_gid, worksheet, config, enabled)
        VALUES ('spreadsheet1', 1, 'tab1', 'config.yaml', true) RETURNING id
        """
    ).fetchone()["id"]
    db.add_job(conn, sheet, 2, "https://example.com/post/1", "example.com")
    coordinator.log_startup(conn)
    assert f"database {conn.info.dbname} on" in caplog.text
    assert f"Tab tab1 (sheet {sheet}, live): spreadsheet spreadsheet1, config config.yaml" in caplog.text
    assert "Queue: 1 queued, 0 running, 1 sheet writes to do, 0 conflicts, 0 unsent alerts" in caplog.text
    assert conn.info.password not in caplog.text


def test_startup_warns_when_no_tabs_are_registered(conn, caplog):
    coordinator.log_startup(conn)
    assert "No tabs registered" in caplog.text


def test_sync_only_runs_when_due(conn, mocker):
    sync = mocker.patch("auto_archiver.job_queue.sync.sync_cycle")
    mocker.patch("auto_archiver.job_queue.writer.writer_cycle")
    coordinator.cycle(conn, sync_due=False)
    sync.assert_not_called()


def test_cleanup_requeues_an_expired_job(conn, mocker):
    mocker.patch("auto_archiver.job_queue.writer.writer_cycle")
    sheet = conn.execute(
        """
        INSERT INTO sheets (spreadsheet_id, worksheet_gid, worksheet, config, enabled)
        VALUES ('spreadsheet1', 1, 'tab1', 'config.yaml', true) RETURNING id
        """
    ).fetchone()["id"]
    job_id = db.add_job(conn, sheet, 2, "https://example.com/post/1", "example.com")
    conn.execute("UPDATE jobs SET available_at = now() - interval '1 minute'")
    db.claim_job(conn, "vm1")
    conn.execute("UPDATE jobs SET lease_until = now() - interval '3 minutes'")
    coordinator.cycle(conn, sync_due=False)
    assert db.get_job(conn, job_id)["status"] == "queued"


def test_migrations_ok_on_an_up_to_date_database(conn, caplog):
    assert coordinator.migrations_ok(conn) is True
    assert "Migrations are up to date" in caplog.text


def test_coordinator_will_not_start_with_pending_migrations(conn, mocker, monkeypatch, database_url, caplog):
    mocker.patch("auto_archiver.job_queue.migrate.check", return_value=(["002_new.sql"], []))
    cycle = mocker.patch("auto_archiver.job_queue.coordinator.cycle")
    with pytest.raises(SystemExit):
        coordinator.main()
    assert "002_new.sql" in caplog.text
    cycle.assert_not_called()


def test_coordinator_will_not_start_with_an_edited_migration(conn, mocker, monkeypatch, database_url, caplog):
    mocker.patch("auto_archiver.job_queue.migrate.check", return_value=([], ["001_initial.sql has been edited"]))
    cycle = mocker.patch("auto_archiver.job_queue.coordinator.cycle")
    with pytest.raises(SystemExit):
        coordinator.main()
    assert "has been edited" in caplog.text
    cycle.assert_not_called()


def test_coordinator_will_not_start_without_the_database(mocker, monkeypatch, caplog):
    mocker.patch("auto_archiver.job_queue.db.connect", side_effect=psycopg.OperationalError("no route"))
    cycle = mocker.patch("auto_archiver.job_queue.coordinator.cycle")
    with pytest.raises(SystemExit):
        coordinator.main()
    assert "can't read the database" in caplog.text
    cycle.assert_not_called()


# the watcher's log lines


def add_sheet(conn) -> int:
    return conn.execute(
        """
        INSERT INTO sheets (spreadsheet_id, worksheet_gid, worksheet, config, enabled)
        VALUES ('spreadsheet1', 1, 'tab1', 'config.yaml', true) RETURNING id
        """
    ).fetchone()["id"]


def test_worker_is_logged_once_when_it_appears_and_when_it_goes_quiet(conn, caplog):
    watcher = coordinator.Watcher()
    db.beat(conn, "worker-supervisor-vm1")
    watcher.watch(conn)
    watcher.watch(conn)
    assert caplog.text.count("Worker vm1 is up") == 1

    conn.execute("UPDATE heartbeats SET beat_at = now() - interval '45 seconds'")
    watcher.watch(conn)
    watcher.watch(conn)
    assert caplog.text.count("Worker vm1 has gone quiet: no heartbeat for 45s") == 1

    db.beat(conn, "worker-supervisor-vm1")
    watcher.watch(conn)
    assert caplog.text.count("Worker vm1 is up") == 2


def test_jobs_from_before_startup_are_not_logged(conn, caplog):
    db.add_job(conn, add_sheet(conn), 2, "https://example.com/post/1", "example.com")
    coordinator.Watcher().watch(conn)
    assert "Queued job" not in caplog.text


def test_a_job_is_logged_when_queued_claimed_and_finished(conn, caplog):
    sheet = add_sheet(conn)
    watcher = coordinator.Watcher()
    watcher.watch(conn)

    job_id = db.add_job(conn, sheet, 2, "https://example.com/post/1", "example.com")
    watcher.watch(conn)
    queued = f"Queued job {job_id} tab tab1 (config) row 2 folder '' https://example.com/post/1"
    assert queued in caplog.text

    conn.execute("UPDATE jobs SET available_at = now() - interval '1 minute'")
    job = db.claim_job(conn, "vm1")
    watcher.watch(conn)
    assert f"Worker vm1 claimed job {job_id} tab tab1 (config) row 2" in caplog.text
    assert "attempt 1, waited " in caplog.text

    cells = {"status": "ok", "archive": "https://example.com/archive/1", "title": "a title"}
    db.finish_job(conn, job_id, job["lease_token"], "done", cells)
    watcher.watch(conn)
    assert (
        f"Job {job_id} on worker vm1 is now done: archive https://example.com/archive/1, title 'a title'" in caplog.text
    )


def test_a_rejected_row_is_logged_with_its_reason(conn, caplog):
    sheet = add_sheet(conn)
    watcher = coordinator.Watcher()
    watcher.watch(conn)
    db.add_rejected_job(conn, sheet, 2, "not a url", "WARNING: not a valid URL")
    watcher.watch(conn)
    assert "row 2 folder '' not a url: WARNING: not a valid URL" in caplog.text


def test_startup_lists_the_workers(conn, caplog):
    db.beat(conn, "worker-supervisor-vm1")
    coordinator.log_startup(conn)
    assert "Worker vm1: last heartbeat 0s ago" in caplog.text
