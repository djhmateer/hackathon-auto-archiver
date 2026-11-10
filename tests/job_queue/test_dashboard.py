"""The console dashboard. Checks what it shows and colours, not the exact layout. The fixtures are in conftest.py"""

import os
import re
import time

import pytest

from auto_archiver.job_queue import dashboard, db
from auto_archiver.job_queue.dashboard import GREEN, RED, YELLOW
from auto_archiver.job_queue.stage import set_stage


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
    titles = ("Auto Archiver queue", "Workers", "Sheets", "Waiting", "Sites", "Storage", "Last 5 failed", "Alerts")
    for title in (*titles, "Last 5 finished"):
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
    line = "\n".join(dashboard.workers(conn, "abc1234"))
    assert f"{GREEN}1{dashboard.RESET} done, {YELLOW}1{dashboard.RESET} failed" in line
    assert f"job {running['id']} Sheet A/tab1 row 4 site3.com, {dashboard.DIM}setting up" in line


def test_a_released_job_is_timed_from_its_new_attempt(conn):
    db.beat(conn, "worker-supervisor-worker41")
    job = run_job(conn, add_sheet(conn), 2, "site1.com")
    conn.execute("UPDATE jobs SET started_at = now() - interval '90 minutes' WHERE id = %s", (job["id"],))
    db.release_worker_jobs(conn, "worker41")
    job = db.claim_job(conn, "worker41")
    db.mark_work_started(conn, job["id"], job["lease_token"])
    line = "\n".join(dashboard.workers(conn, "abc1234"))
    assert f"site1.com, {GREEN}0s{dashboard.RESET}, attempt 1" in line


def stage_of(conn, job_id):
    return conn.execute("SELECT stage FROM jobs WHERE id = %s", (job_id,)).fetchone()["stage"]


def test_a_running_job_shows_its_stage(conn, monkeypatch):
    db.beat(conn, "worker-supervisor-worker41")
    job = run_job(conn, add_sheet(conn), 2, "site1.com")
    monkeypatch.setenv("AA_JOB_ID", str(job["id"]))
    set_stage("wacz_extractor_enricher")
    line = "\n".join(dashboard.workers(conn, "abc1234"))
    assert "  wacz_extractor_enricher, 0s in this stage" in line


def test_a_new_attempt_starts_with_no_stage(conn, monkeypatch):
    job = run_job(conn, add_sheet(conn), 2, "site1.com")
    monkeypatch.setenv("AA_JOB_ID", str(job["id"]))
    set_stage("storing media 1 of 10")
    db.release_worker_jobs(conn, "worker41")
    db.claim_job(conn, "worker41")
    assert stage_of(conn, job["id"]) is None


def test_set_stage_does_nothing_outside_a_queue_job(conn, monkeypatch):
    job = run_job(conn, add_sheet(conn), 2, "site1.com")
    monkeypatch.delenv("AA_JOB_ID", raising=False)
    set_stage("generic_extractor")
    assert stage_of(conn, job["id"]) is None


def test_set_stage_leaves_a_job_that_is_no_longer_running(conn, monkeypatch):
    job = run_job(conn, add_sheet(conn), 2, "site1.com")
    finish(conn, job, "done", "wacz: success")
    monkeypatch.setenv("AA_JOB_ID", str(job["id"]))
    set_stage("html_formatter")
    assert stage_of(conn, job["id"]) is None


def start_work(conn, sheet_id, row, monkeypatch, domain="site1.com", worker="worker41") -> dict:
    job = run_job(conn, sheet_id, row, domain, worker)
    db.mark_work_started(conn, job["id"], job["lease_token"])
    monkeypatch.setenv("AA_JOB_ID", str(job["id"]))
    return job


def steps(conn, job_id) -> list[tuple]:
    rows = conn.execute(
        "SELECT attempt, step, ended_at IS NOT NULL AS ended FROM job_stages WHERE job_id = %s ORDER BY id", (job_id,)
    ).fetchall()
    return [(r["attempt"], r["step"], r["ended"]) for r in rows]


def test_each_step_is_timed_and_progress_does_not_start_a_new_one(conn, monkeypatch):
    job = start_work(conn, add_sheet(conn), 2, monkeypatch)
    set_stage("instagram_api_extractor")
    conn.execute("UPDATE jobs SET stage_at = now() - interval '10 minutes' WHERE id = %s", (job["id"],))
    set_stage("instagram_api_extractor", "posts 30, 50 of 1000 limit")
    row = conn.execute(
        "SELECT stage, now() - stage_at > interval '9 minutes' AS kept FROM jobs WHERE id = %s", (job["id"],)
    ).fetchone()
    assert row["stage"] == "instagram_api_extractor: posts 30, 50 of 1000 limit"
    assert row["kept"]  # still timed from the step's start
    set_stage("storing", "media 1 of 10")
    assert steps(conn, job["id"]) == [(1, "instagram_api_extractor", True), (1, "storing", False)]


def test_finishing_ends_the_last_step(conn, monkeypatch):
    job = start_work(conn, add_sheet(conn), 2, monkeypatch)
    set_stage("html_formatter")
    finish(conn, job, "done", "wacz: success")
    assert steps(conn, job["id"]) == [(1, "html_formatter", True)]


def test_a_killed_attempt_leaves_its_step_open(conn, monkeypatch):
    job = start_work(conn, add_sheet(conn), 2, monkeypatch)
    set_stage("wacz_extractor_enricher")
    conn.execute("UPDATE job_stages SET started_at = now() - interval '1 minute'")
    db.release_worker_jobs(conn, "worker41")
    from auto_archiver.job_queue import stage

    stage._step = None  # the new attempt is a new archiver process
    job = db.claim_job(conn, "worker41")
    conn.execute("UPDATE jobs SET work_started_at = now() WHERE id = %s", (job["id"],))
    set_stage("instagram_api_extractor")
    set_stage("hash_enricher")
    # the release gave the attempt back, so both attempts are 1
    assert steps(conn, job["id"]) == [
        (1, "wacz_extractor_enricher", False),
        (1, "instagram_api_extractor", True),
        (1, "hash_enricher", False),
    ]


def test_peaks_keep_the_lowest_memory_and_disk_of_the_jobs_own_worker(conn):
    job = run_job(conn, add_sheet(conn), 2, "site1.com")
    db.record_peaks(conn, job["id"], "worker41", {"mem_available_bytes": 3 * GB, "disk_free_bytes": 5 * GB})
    db.record_peaks(conn, job["id"], "worker41", {"mem_available_bytes": 2 * GB, "disk_free_bytes": 9 * GB})
    db.record_peaks(conn, job["id"], "worker42", {"mem_available_bytes": 1, "disk_free_bytes": 1})  # not its worker
    row = conn.execute("SELECT * FROM jobs WHERE id = %s", (job["id"],)).fetchone()
    assert (row["min_mem_available_bytes"], row["min_disk_free_bytes"]) == (2 * GB, 5 * GB)


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


def test_waiting_jobs_with_no_organisation_or_pool_worker_say_so(conn):
    no_org = add_sheet(conn, gid=1)
    conn.execute("UPDATE sheets SET organisation_id = NULL WHERE id = %s", (no_org,))
    cir = conn.execute("INSERT INTO organisations (name, pool) VALUES ('Org C', 'cir') RETURNING id").fetchone()["id"]
    no_worker = add_sheet(conn, gid=2)
    conn.execute("UPDATE sheets SET organisation_id = %s WHERE id = %s", (cir, no_worker))
    add_job(conn, no_org, 2, "site1.com")
    add_job(conn, no_worker, 3, "site2.com")
    screen = "\n".join(dashboard.waiting(conn))
    assert "row 2 site1.com: " + YELLOW + "sheet has no organisation, so no pool" in screen
    assert "row 3 site2.com: " + YELLOW + "no worker in its pool" in screen
    sheets = "\n".join(dashboard.sheets(conn))
    assert RED + "none " in sheets  # the sheet with no organisation
    assert "cir  " in sheets


def test_a_worker_with_no_pool_is_red(conn):
    db.beat(conn, "worker-supervisor-new-vm")
    screen = "\n".join(dashboard.workers(conn, "abc1234"))
    assert "pool " + RED + "none, so it claims nothing" in screen


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
    assert f"Last backup: {GREEN}" in "\n".join(dashboard.header(conn, {}))
    two_days_ago = time.time() - 2 * 24 * 60 * 60
    os.utime(backup, (two_days_ago, two_days_ago))
    assert f"Last backup: {RED}2d 0h ago" in "\n".join(dashboard.header(conn, {}))


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


GB = dashboard.GB
STATS = {
    "cpu_percent": 12.3,
    "disk_free_bytes": 20 * GB,
    "mem_available_bytes": 2 * GB,
    "mem_total_bytes": 4 * GB,
    "swap_used_bytes": 0,
}


def test_a_worker_shows_its_code_and_machine(conn):
    db.beat(conn, "worker-supervisor-worker41", "abc1234", STATS)
    db.beat(conn, "worker-supervisor-worker42", "old5678", {**STATS, "disk_free_bytes": 3 * GB})
    db.beat(conn, "worker-supervisor-worker43")  # not deployed since the heartbeat details were added
    screen = "\n".join(dashboard.workers(conn, "abc1234"))
    assert f"code {GREEN}abc1234" in screen
    assert f"code {RED}old5678" in screen  # missed a deploy
    assert f"code {YELLOW}?" in screen
    assert (
        f"disk {GREEN}20.0 GB{dashboard.RESET} free, memory {GREEN}[█████░░░░░] 2.0 GB used, 2.0 GB{dashboard.RESET} available"
        in screen
    )
    assert f"disk {RED}3.0 GB" in screen
    assert "cpu, disk, memory and swap ?" in screen


@pytest.mark.parametrize(
    "stats, expected",
    [
        ({"disk_free_bytes": 8 * GB}, f"disk {YELLOW}8.0 GB"),
        ({"mem_available_bytes": GB // 2}, f"memory {YELLOW}[█████████░] 3.5 GB used, 0.5 GB"),
        ({"mem_available_bytes": GB // 5}, f"memory {RED}[██████████] 3.8 GB used, 0.2 GB"),
        ({"swap_used_bytes": 2 * GB}, f"swap {YELLOW}2048 MB"),
        ({"cpu_percent": 12.3}, f"cpu {GREEN}12%"),
        ({"cpu_percent": 95.0}, f"cpu {YELLOW}95%"),
        ({"cpu_percent": None}, f"cpu {GREEN}?"),
    ],
)
def test_machine_colours(stats, expected):
    assert expected in dashboard.machine({**STATS, **stats})


def test_the_coordinators_machine_is_in_the_header(conn):
    screen = "\n".join(dashboard.header(conn, STATS))
    assert f"disk {GREEN}20.0 GB" in screen


def test_speed_counts_finished_jobs_and_the_oldest_queued(conn):
    sheet_id = add_sheet(conn)
    finish(conn, run_job(conn, sheet_id, 2, "site1.com"), "done", "wacz: success")
    old = add_job(conn, sheet_id, 3, "site2.com")
    conn.execute("UPDATE jobs SET added_at = now() - interval '2 hours' WHERE id = %s", (old,))
    screen = "\n".join(dashboard.header(conn, STATS))
    assert (
        "Speed: 1 finished in the last hour, 1 in the last 24h, oldest queued job added " + YELLOW + "2h 0m" in screen
    )


def test_sites_count_done_failed_and_blocked(conn):
    sheet_id = add_sheet(conn)
    finish(conn, run_job(conn, sheet_id, 2, "site1.com"), "done", "wacz: success")
    finish(conn, run_job(conn, sheet_id, 3, "site1.com"), "failed", "Archive failed no archiver")
    finish(conn, run_job(conn, sheet_id, 4, "site1.com"), "done", "no archiver (blocked by site - please requeue)")
    finish(conn, run_job(conn, sheet_id, 5, "site2.com"), "done", "wacz: success")
    lines = dashboard.sites(conn)
    plain = [re.sub(r"\033\[\d+m", "", line) for line in lines]
    assert plain[2].split()[:4] == ["site1.com", "1", "1", "1"]  # busiest first
    assert plain[3].split()[:4] == ["site2.com", "1", "0", "0"]
    assert RED + "      1" in lines[2]  # blocked


def test_storage_totals_files_and_flags_failures(conn):
    sheet_id = add_sheet(conn)
    job = run_job(conn, sheet_id, 2, "site1.com")
    db.finish_job(
        conn,
        job["id"],
        job["lease_token"],
        "done",
        {"status": "wacz: success"},
        result={"store_failures": 2},
        files=[{"role": "", "key": "a/1.mp4", "bytes": GB}, {"role": "screenshot", "key": "a/2.png", "bytes": GB}],
    )
    other_sheet = add_sheet(conn, gid=2)
    conn.execute("UPDATE sheets SET worksheet = 'tab2' WHERE id = %s", (other_sheet,))
    other_job = run_job(conn, other_sheet, 2, "site2.com")
    db.finish_job(
        conn,
        other_job["id"],
        other_job["lease_token"],
        "done",
        {"status": "wacz: success"},
        files=[{"role": "", "key": "b/1.mp4", "bytes": 3 * GB}],
    )
    lines = dashboard.storage(conn)
    assert "Storage (24h): 3 files, 5.00 GB stored" in lines[0]
    assert f"{RED}1 job(s) with files that couldn't be stored: job {job['id']} (2)" in lines[0]
    # a line per tab, biggest first
    assert lines[1].split() == ["Sheet", "A/tab2", "1", "files", "3.00", "GB"]
    assert lines[2].split() == ["Sheet", "A/tab1", "2", "files", "2.00", "GB"]


def test_storage_lists_every_tab_that_is_on_even_with_nothing_stored(conn):
    add_sheet(conn, gid=1)
    add_sheet(conn, gid=2, enabled=False)
    lines = dashboard.storage(conn)
    assert len(lines) == 2  # the tab that's off isn't listed
    assert lines[1].split() == ["Sheet", "A/tab1", "0", "files", "0.00", "GB"]


def test_failures_lists_only_failed_and_blocked_jobs(conn):
    sheet_id = add_sheet(conn)
    finish(conn, run_job(conn, sheet_id, 2, "site1.com"), "done", "wacz: success")
    finish(conn, run_job(conn, sheet_id, 3, "site2.com"), "failed", "Archive failed no archiver")
    finish(conn, run_job(conn, sheet_id, 4, "site3.com"), "done", "no archiver (blocked by site - please requeue)")
    screen = "\n".join(dashboard.failures(conn))
    assert "site1.com" not in screen
    assert "row 3 site2.com" in screen and "row 4 site3.com" in screen
