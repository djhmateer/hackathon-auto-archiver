"""The console dashboard. Checks what it shows and colours, not the exact layout. The fixtures are in conftest.py"""

import os
import re
import time

import pytest

from auto_archiver.job_queue import dashboard, db
from auto_archiver.job_queue.dashboard import GREEN, RED, YELLOW


@pytest.fixture(autouse=True)
def backup_dir(tmp_path, monkeypatch):
    folder = tmp_path / "backups"
    folder.mkdir()
    monkeypatch.setattr(dashboard, "BACKUP_DIR", folder)
    return folder


def add_sheet(conn, gid=1, enabled=True) -> int:
    return conn.execute(
        """
        INSERT INTO sheets (spreadsheet_id, worksheet_gid, worksheet, config, enabled, spreadsheet_name)
        VALUES ('spreadsheet1', %s, 'tab1', 'secrets/orchestration-example.yaml', %s, 'Sheet A') RETURNING id
        """,
        (gid, enabled),
    ).fetchone()["id"]


def add_job(conn, sheet_id, row, domain) -> int:
    job_id = db.add_job(conn, sheet_id, row, f"https://{domain}/post/{row}", domain)
    conn.execute("UPDATE jobs SET available_at = now() - interval '1 minute' WHERE id = %s", (job_id,))
    return job_id


def run_job(conn, sheet_id, row, domain, worker="worker41") -> dict:
    add_job(conn, sheet_id, row, domain)
    return db.claim_job(conn, worker)


def finish(conn, job, status, sheet_status) -> None:
    db.finish_job(conn, job["id"], job["lease_token"], status, {"status": sheet_status})


def test_an_empty_queue_renders_every_section(conn):
    screen = dashboard.render(conn)
    for title in ("Auto Archiver queue", "Workers", "Sheets", "Waiting", "Last 10 finished", "Alerts"):
        assert title in screen
    assert f"{RED}no heartbeat ever" in screen
    assert f"{RED}  No worker has ever sent a heartbeat" in screen
    assert f"{RED}none in " in screen  # no backup


def test_heartbeats_are_green_when_recent_and_red_when_quiet(conn):
    db.beat(conn, "coordinator")
    db.beat(conn, "worker-supervisor-worker41")
    db.beat(conn, "worker-supervisor-worker42")
    conn.execute("UPDATE heartbeats SET beat_at = now() - interval '5 minutes' WHERE name LIKE '%worker42'")
    screen = dashboard.render(conn)
    assert f"Coordinator: heartbeat {GREEN}0s ago" in screen
    worker41, worker42 = [line for line in screen.splitlines() if "heartbeat" in line and "worker4" in line]
    assert f"{GREEN}0s ago" in worker41
    assert f"{RED}5m ago" in worker42


def test_a_worker_shows_its_running_job_and_todays_counts(conn):
    db.beat(conn, "worker-supervisor-worker41")
    sheet_id = add_sheet(conn)
    finish(conn, run_job(conn, sheet_id, 2, "site1.com"), "done", "wacz: success")
    finish(conn, run_job(conn, sheet_id, 3, "site2.com"), "failed", "Archive failed no archiver")
    running = run_job(conn, sheet_id, 4, "site3.com")
    line = next(line for line in dashboard.workers(conn) if "worker41" in line)
    assert f"{GREEN}1{dashboard.RESET} done, {YELLOW}1{dashboard.RESET} failed" in line
    assert f"job {running['id']} Sheet 'Sheet A' tab tab1 row 4 site3.com" in line


def test_finished_jobs_show_the_sheet_status_and_blocks_are_yellow(conn):
    sheet_id = add_sheet(conn)
    finish(conn, run_job(conn, sheet_id, 2, "site1.com"), "done", "wacz: success")
    finish(conn, run_job(conn, sheet_id, 3, "site2.com"), "done", "no archiver (blocked by site - please requeue)")
    lines = dashboard.recent(conn)
    assert f"{YELLOW}no archiver (blocked by site" in lines[1]  # newest first
    assert f"{GREEN}wacz: success" in lines[2]
    assert "https://" not in "\n".join(lines)  # only the domain


def test_sheets_show_counts_and_state(conn):
    on = add_sheet(conn, gid=1)
    add_sheet(conn, gid=2, enabled=False)
    paused = add_sheet(conn, gid=3)
    conn.execute("UPDATE sheets SET paused_until = now() + interval '10 minutes' WHERE id = %s", (paused,))
    add_job(conn, on, 2, "site1.com")
    lines = dashboard.sheets(conn)
    assert len(lines) == 5
    # the headings line up over the numbers
    heading, first = (re.sub(r"\033\[\d+m", "", line) for line in lines[1:3])
    assert heading.index("queued") + len("queued") == first.index("     1 ") + len("     1")
    assert any("    1 " in line and f"{GREEN}on" in line for line in lines)
    assert any(f"{RED}paused until" in line for line in lines)
    assert any("off" in line for line in lines)


def test_waiting_jobs_say_why(conn):
    sheet_id = add_sheet(conn, gid=1)
    off_sheet = add_sheet(conn, gid=2, enabled=False)
    run_job(conn, sheet_id, 2, "busy.com")
    add_job(conn, sheet_id, 3, "busy.com")
    retry = add_job(conn, sheet_id, 4, "site1.com")
    conn.execute("UPDATE jobs SET available_at = now() + interval '1 hour' WHERE id = %s", (retry,))
    add_job(conn, off_sheet, 5, "site2.com")
    add_job(conn, sheet_id, 6, "free.com")  # claimable, so not shown
    screen = "\n".join(dashboard.waiting(conn))
    assert "3 queued job(s) that can't be claimed yet" in screen
    assert "row 3 busy.com: " + YELLOW + "site busy" in screen
    assert "row 4 site1.com: " + YELLOW + "retry at" in screen
    assert "row 5 site2.com: " + YELLOW + "sheet is off" in screen
    assert "free.com" not in screen


def test_alerts_show_unsent_in_red_and_only_the_first_line(conn):
    db.add_alert(conn, "site_blocked", "site1.com blocked tab tab1 row 2\n\nURL: https://site1.com/post/2")
    db.add_alert(conn, "sheet_paused", "Sheet paused")
    conn.execute("UPDATE admin_alerts SET sent_at = now() WHERE kind = 'sheet_paused'")
    screen = "\n".join(dashboard.alerts(conn))
    assert f"{RED}" in screen and "site_blocked: site1.com blocked tab tab1 row 2  (unsent)" in screen
    assert "sheet_paused: Sheet paused" in screen
    assert "https://" not in screen


def test_backup_is_green_when_recent_and_red_when_old(conn, backup_dir):
    backup = backup_dir / "auto_archiver_Mon.dump"
    backup.write_bytes(b"x")
    assert f"Last backup: {GREEN}" in "\n".join(dashboard.header(conn))
    two_days_ago = time.time() - 2 * 24 * 60 * 60
    os.utime(backup, (two_days_ago, two_days_ago))
    assert f"Last backup: {RED}2d 0h ago" in "\n".join(dashboard.header(conn))


@pytest.mark.parametrize(
    "seconds, text", [(5, "5s"), (125, "2m"), (3 * 3600 + 300, "3h 5m"), (2 * 86400 + 4 * 3600, "2d 4h")]
)
def test_ago(seconds, text):
    assert dashboard.ago(seconds) == text


def test_sheets_show_what_sync_last_read(conn):
    read = add_sheet(conn, gid=1)
    add_sheet(conn, gid=2)  # on, but never read
    late = add_sheet(conn, gid=3)
    add_sheet(conn, gid=4, enabled=False)
    db.save_tab_read(conn, read, 5907, 3)
    db.save_tab_read(conn, late, 10, 0)
    conn.execute("UPDATE sheets SET last_read_at = now() - interval '5 minutes' WHERE id = %s", (late,))
    lines = dashboard.sheets(conn)[2:]
    assert "  5907      3   " + GREEN + "0s ago" in lines[0]
    assert RED + "never" in lines[1]
    assert RED + "5m ago" in lines[2]
    assert dashboard.DIM + "never" in lines[3]
