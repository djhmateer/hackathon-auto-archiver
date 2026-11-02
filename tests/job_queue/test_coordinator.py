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
