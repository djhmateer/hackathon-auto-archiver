"""The morning report. Postmark is mocked: nothing is sent. The fixtures are in conftest.py"""

import sys

import pytest

from auto_archiver.job_queue import dashboard, db, report

GB = dashboard.GB
STATS = {"cpu_percent": 10.0, "disk_free_bytes": 20 * GB, "mem_available_bytes": 2 * GB, "mem_total_bytes": 4 * GB}
STATS["swap_used_bytes"] = 0


@pytest.fixture(autouse=True)
def fresh_backup(tmp_path, monkeypatch):
    folder = tmp_path / "backups"
    folder.mkdir()
    (folder / "auto_archiver_Mon.dump").write_bytes(b"x")
    monkeypatch.setattr(dashboard, "BACKUP_DIR", folder)


def add_sheet(conn, gid=1, name="Sheet A", client="client1") -> int:
    sheet_id = conn.execute(
        """
        INSERT INTO sheets (spreadsheet_id, worksheet_gid, worksheet, config, enabled, spreadsheet_name, client)
        VALUES ('spreadsheet1', %s, 'tab1', 'secrets/orchestration-example.yaml', true, %s, %s) RETURNING id
        """,
        (gid, name, client),
    ).fetchone()["id"]
    db.save_tab_read(conn, sheet_id, 10, 0)
    return sheet_id


def run_job(conn, sheet_id, row, domain, status="done", sheet_status="wacz: success", worker="glanworker1", **extra):
    job_id = db.add_job(conn, sheet_id, row, f"https://{domain}/post/{row}", domain)
    conn.execute("UPDATE jobs SET available_at = now() - interval '1 minute' WHERE id = %s", (job_id,))
    job = db.claim_job(conn, worker)
    db.finish_job(conn, job["id"], job["lease_token"], status, {"status": sheet_status}, **extra)
    return job["id"]


def build(conn, commit="abc1234", stats=STATS):
    return report.build(conn, 24, commit, stats)


def test_a_quiet_day_needs_no_attention(conn):
    db.beat(conn, "worker-supervisor-glanworker1", "abc1234", STATS)
    run_job(conn, add_sheet(conn), 2, "site1.com")
    subject, body = build(conn)
    assert subject.endswith(": nothing needs attention")
    assert "Problems\n  none\n" in body
    assert "1 job(s) finished: 1 done, 0 failed, 0 blocked by the site" in body


def test_job_problems_are_listed_with_their_ids(conn):
    sheet_id = add_sheet(conn)
    failed = run_job(conn, sheet_id, 2, "site1.com", "failed", "Archive failed no archiver", error="no archiver")
    killed = run_job(
        conn,
        sheet_id,
        3,
        "site2.com",
        "failed",
        "Archive failed - killed",
        error="- still running after 185 minutes, killed",
    )
    run_job(conn, sheet_id, 4, "site3.com", "done", "no archiver (blocked by site - please requeue)")
    unstored = run_job(conn, sheet_id, 5, "site4.com", result={"store_failures": 1})
    subject, body = build(conn)
    assert subject.endswith(": 4 problem(s)")
    assert f"- 1 job(s) failed (site1.com 1): jobs {failed}" in body
    assert f"- 1 job(s) killed at the time limit: jobs {killed}" in body
    assert "- 1 job(s) blocked by the site (site3.com 1)" in body
    assert f"- 1 job(s) with files that couldn't be stored: jobs {unstored}" in body


def test_machine_and_queue_problems(conn):
    sheet_id = add_sheet(conn)
    conn.execute("UPDATE sheets SET last_read_at = now() - interval '1 hour' WHERE id = %s", (sheet_id,))
    db.beat(conn, "worker-supervisor-glanworker1", "old5678", {**STATS, "disk_free_bytes": 3 * GB})
    db.beat(conn, "worker-supervisor-glanworker2", "abc1234", STATS)
    conn.execute("UPDATE heartbeats SET beat_at = now() - interval '10 minutes' WHERE name LIKE '%glanworker2'")
    old = db.add_job(conn, sheet_id, 9, "https://site9.com/post/9", "site9.com")
    conn.execute("UPDATE jobs SET added_at = now() - interval '7 hours' WHERE id = %s", (old,))
    db.add_alert(conn, "sheet_paused", "Sheet paused")
    _, body = build(conn, stats={**STATS, "mem_available_bytes": GB // 10})
    assert "- Sheet A/tab1 is on but sync has read it not since " in body
    assert "- Worker glanworker1 runs code old5678, the coordinator abc1234" in body
    assert "- Worker glanworker1 has 3.0 GB of disk free" in body
    assert "- Worker glanworker2 has sent no heartbeat for 10m" in body
    assert "- The coordinator has 0.1 GB of memory available" in body
    assert "- 1 job(s) queued, the oldest added 7h 0m ago" in body
    assert "- Alerts raised: sheet_paused 1 (listed below)" in body
    assert "- 1 alert(s) that should have been emailed are still unsent" in body


def test_a_missing_backup_is_a_problem(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "BACKUP_DIR", tmp_path / "nowhere")
    _, body = build(conn)
    assert "- No database backup in " in body


def test_detail_has_sheets_sites_workers_storage_and_alerts_but_no_urls(conn):
    sheet_id = add_sheet(conn)
    for row in (2, 3):
        run_job(conn, sheet_id, row, "site1.com", files=[{"role": "", "key": f"a/{row}.mp4", "bytes": GB}])
    run_job(conn, sheet_id, 4, "site2.com", "failed", "Archive failed no archiver", worker="glanworker2")
    db.add_alert(conn, "site_blocked", "site1.com blocked tab tab1 row 2\n\nURL: https://site1.com/post/2")
    _, body = build(conn)
    lines = body.splitlines()

    def row_of(section, name):
        start = lines.index(section)
        return next(line.split() for line in lines[start:] if line.strip().startswith(name))

    assert row_of("Sheets", "Sheet A/tab1") == ["Sheet", "A/tab1", "2", "1", "0", "0"]
    assert row_of("Sites", "site1.com")[:4] == ["site1.com", "2", "0", "0"]
    assert row_of("Workers", "glanworker1")[:2] == ["glanworker1", "2"]
    assert row_of("Storage", "Sheet A/tab1")[2:] == ["client1", "2", "2.00"]
    assert "site_blocked: site1.com blocked tab tab1 row 2" in body
    assert "https://" not in body


def test_main_saves_and_emails_it(conn, tmp_path, monkeypatch, mocker):
    monkeypatch.setattr(report, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(report, "git_commit", lambda: "abc1234")
    monkeypatch.setattr(report, "machine_stats", lambda path: STATS)
    with open(tmp_path / ".env", "a") as f:
        f.write("POSTMARK_API_TOKEN=token1\nPOSTMARK_FROM_EMAIL=from@example.com\nALERT_EMAIL_TO=to@example.com\n")
    send = mocker.patch.object(report.alerts, "send_email")
    monkeypatch.setattr(sys, "argv", ["report"])
    report.main()
    token, from_email, to, subject, body = send.call_args.args
    assert (token, from_email, to) == ("token1", "from@example.com", "to@example.com")
    assert subject.startswith("Auto Archiver report ")
    (saved,) = (tmp_path / "reports").glob("report_*.txt")
    assert saved.read_text() == f"{subject}\n\n{body}"


def test_no_email_only_prints_and_saves(conn, tmp_path, monkeypatch, mocker):
    monkeypatch.setattr(report, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(report, "git_commit", lambda: "abc1234")
    monkeypatch.setattr(report, "machine_stats", lambda path: STATS)
    send = mocker.patch.object(report.alerts, "send_email")
    monkeypatch.setattr(sys, "argv", ["report", "--no-email"])
    report.main()
    send.assert_not_called()
    assert list((tmp_path / "reports").glob("report_*.txt"))
