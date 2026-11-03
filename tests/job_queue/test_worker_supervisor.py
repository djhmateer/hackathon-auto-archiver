"""The supervisor's rules against real Postgres, with fake child processes and a clock the tests control"""

import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from auto_archiver.job_queue import db, worker_supervisor
from auto_archiver.job_queue.worker_supervisor import WorkerSupervisor


class FakeProcess:
    def __init__(self):
        self.exit_code = None
        self.killed = False

    def poll(self):
        return self.exit_code

    def kill(self):
        self.killed = True
        self.exit_code = -9


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def no_docker(mocker):
    return mocker.patch("auto_archiver.job_queue.worker_supervisor.run_docker", return_value="")


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def children():
    return []


@pytest.fixture
def make_supervisor(tmp_path, clock, children, mocker):
    def start(job, worker_dir):
        children.append(FakeProcess())
        return children[-1]

    def make(**kwargs):
        options = {
            "worker": "vm1",
            "start": start,
            "kill": mocker.MagicMock(),
            "clock": clock,
        }
        return WorkerSupervisor(**{**options, **kwargs})

    return make


def add_sheet(conn, gid=1) -> int:
    return conn.execute(
        """
        INSERT INTO sheets (spreadsheet_id, worksheet_gid, worksheet, config, enabled)
        VALUES ('spreadsheet1', %s, 'tab1', 'orchestration-example.yaml', true) RETURNING id
        """,
        (gid,),
    ).fetchone()["id"]


def add_job(conn, sheet_id, row=2) -> int:
    job_id = db.add_job(conn, sheet_id, row, f"https://example.com/post/{row}", "example.com")
    conn.execute("UPDATE jobs SET available_at = now() - interval '1 minute' WHERE id = %s", (job_id,))
    return job_id


def job(conn, job_id) -> dict:
    return db.get_job(conn, job_id)


def last_cells(conn, job_id) -> dict:
    return conn.execute(
        "SELECT cells FROM sheet_writes WHERE job_id = %s ORDER BY id DESC LIMIT 1", (job_id,)
    ).fetchone()["cells"]


def alert_kinds(conn) -> list[str]:
    return [a["kind"] for a in conn.execute("SELECT kind FROM admin_alerts ORDER BY id").fetchall()]


# claiming and renewing


def test_idle_supervisor_claims_and_starts_a_child(conn, make_supervisor, children):
    job_id = add_job(conn, add_sheet(conn))
    sup = make_supervisor()
    sup.tick(conn)
    assert len(children) == 1
    assert sup.running.job["id"] == job_id
    assert job(conn, job_id)["status"] == "running"
    assert job(conn, job_id)["worker"] == "vm1"


def test_one_job_runs_at_a_time_and_the_rest_wait(conn, make_supervisor, children):
    sheet = add_sheet(conn)
    for row in range(2, 5):
        add_job(conn, sheet, row)
    sup = make_supervisor()
    sup.tick(conn)
    sup.tick(conn)
    assert len(children) == 1
    assert conn.execute("SELECT count(*) AS n FROM jobs WHERE status = 'queued'").fetchone()["n"] == 2


def test_idle_line_is_only_logged_when_it_changes(conn, make_supervisor, clock, caplog):
    sup = make_supervisor()
    sup.tick(conn)
    clock.now += 600
    sup.tick(conn)
    assert caplog.text.count("Waiting for jobs: none to claim. On all workers 0 queued, 0 running") == 1

    # a queued job it can't claim yet changes the line
    add_job(conn, add_sheet(conn))
    conn.execute("UPDATE jobs SET available_at = now() + interval '1 hour'")
    sup.tick(conn)
    assert "1 queued (waiting for their site to be free, a retry, or a paused or disabled sheet)" in caplog.text


def test_going_idle_again_after_a_job_is_logged(conn, make_supervisor, children, caplog):
    sup = make_supervisor()
    sup.tick(conn)
    job_id = add_job(conn, add_sheet(conn))
    sup.tick(conn)
    db.finish_job(conn, job_id, sup.running.job["lease_token"], "done", {"status": "ok"})
    children[0].exit_code = 0
    sup.tick(conn)
    assert caplog.text.count("Waiting for jobs: none to claim. On all workers 0 queued, 0 running") == 2


def test_started_line_has_the_attempt_and_config(conn, make_supervisor, caplog):
    add_job(conn, add_sheet(conn))
    make_supervisor().tick(conn)
    assert "attempt 1, config orchestration-example" in caplog.text
    assert "orchestration-example.yaml" not in caplog.text


def test_lease_is_renewed_every_minute(conn, make_supervisor, clock):
    job_id = add_job(conn, add_sheet(conn))
    sup = make_supervisor()
    sup.tick(conn)
    conn.execute("UPDATE jobs SET lease_until = now() - interval '1 minute' WHERE id = %s", (job_id,))

    clock.now += 30
    sup.tick(conn)
    assert conn.execute("SELECT lease_until < now() AS old FROM jobs").fetchone()["old"], "not due yet"

    clock.now += 31
    sup.tick(conn)
    assert not conn.execute("SELECT lease_until < now() AS old FROM jobs").fetchone()["old"]


def test_connection_error_on_renewal_keeps_the_child_running(conn, make_supervisor, clock, children, mocker):
    add_job(conn, add_sheet(conn))
    sup = make_supervisor()
    sup.tick(conn)
    clock.now += 61
    mocker.patch("auto_archiver.job_queue.db.renew_lease", side_effect=OSError("Postgres has gone"))
    with pytest.raises(OSError):
        sup.tick(conn)
    assert children[0].killed is False
    assert sup.running.process is children[0]


def test_stolen_lease_kills_the_child_and_changes_nothing(conn, make_supervisor, clock, children):
    job_id = add_job(conn, add_sheet(conn))
    sup = make_supervisor()
    sup.tick(conn)
    new_token = uuid4()
    conn.execute("UPDATE jobs SET lease_token = %s, worker = 'vm2' WHERE id = %s", (new_token, job_id))

    clock.now += 61
    sup.tick(conn)
    assert children[0].killed is True
    sup.tick(conn)

    current = job(conn, job_id)
    assert (current["status"], current["worker"], current["lease_token"]) == ("running", "vm2", new_token)
    assert sup.running is None


def test_a_child_that_has_saved_its_result_but_not_exited_is_left_alone(conn, make_supervisor, clock, children, caplog):
    job_id = add_job(conn, add_sheet(conn))
    sup = make_supervisor()
    sup.tick(conn)
    token = job(conn, job_id)["lease_token"]
    db.finish_job(conn, job_id, token, "done", {"status": "done"})

    clock.now += 61
    sup.tick(conn)
    assert children[0].killed is False
    assert "taken by another worker" not in caplog.text

    children[0].exit_code = 0
    sup.tick(conn)
    assert f"Finished job {job_id}: done" in caplog.text
    assert sup.running is None


# exits


def test_exit_during_setup_logs_the_exit_code(conn, make_supervisor, children, caplog):
    add_job(conn, add_sheet(conn))
    sup = make_supervisor()
    sup.tick(conn)
    children[0].exit_code = -11
    sup.tick(conn)
    assert "exited during setup with code -11" in caplog.text


def test_clean_finish_lets_it_claim_the_next_job(conn, make_supervisor, children, caplog):
    sheet = add_sheet(conn)
    first = add_job(conn, sheet, 2)
    second = add_job(conn, sheet, 3)
    sup = make_supervisor()
    sup.tick(conn)
    token = sup.running.job["lease_token"]
    db.finish_job(conn, first, token, "done", {"status": "ok"})
    children[0].exit_code = 0
    sup.tick(conn)
    assert sup.running.job["id"] == second
    assert job(conn, first)["status"] == "done"
    assert f"Finished job {first}: done in 0m 0s" in caplog.text


def test_exit_during_setup_requeues_and_gives_back_the_attempt(conn, make_supervisor, children):
    job_id = add_job(conn, add_sheet(conn))
    sup = make_supervisor()
    sup.tick(conn)
    children[0].exit_code = 1
    sup.tick(conn)
    current = job(conn, job_id)
    assert (current["status"], current["attempts"]) == ("queued", 0)
    assert len(children) == 1, "the 15 minute delay stops it being claimed straight away"


def test_exit_after_work_started_is_a_final_failure(conn, make_supervisor, children):
    job_id = add_job(conn, add_sheet(conn))
    sup = make_supervisor()
    sup.tick(conn)
    db.mark_work_started(conn, job_id, sup.running.job["lease_token"])
    children[0].exit_code = -9
    sup.tick(conn)
    assert job(conn, job_id)["status"] == "failed"
    assert last_cells(conn, job_id)["status"] == "Archive failed - the archiver stopped unexpectedly - please rerun"


def test_child_that_reported_its_own_failure_is_left_as_it_is(conn, make_supervisor, children, caplog):
    job_id = add_job(conn, add_sheet(conn))
    sup = make_supervisor()
    sup.tick(conn)
    token = sup.running.job["lease_token"]
    db.mark_work_started(conn, job_id, token)
    db.finish_job(conn, job_id, token, "failed", {"status": "Archive failed its own reason"}, "its own reason")
    children[0].exit_code = 1
    sup.tick(conn)
    assert last_cells(conn, job_id)["status"] == "Archive failed its own reason"
    assert f"Finished job {job_id}: failed in 0m 0s its own reason" in caplog.text


def test_exit_after_the_job_was_taken_changes_nothing(conn, make_supervisor, children):
    job_id = add_job(conn, add_sheet(conn))
    sup = make_supervisor()
    sup.tick(conn)
    new_token = uuid4()
    conn.execute("UPDATE jobs SET lease_token = %s, worker = 'vm2' WHERE id = %s", (new_token, job_id))
    children[0].exit_code = 1
    sup.tick(conn)
    assert (job(conn, job_id)["status"], job(conn, job_id)["lease_token"]) == ("running", new_token)


def test_postgres_failing_during_an_exit_is_tried_again(conn, make_supervisor, children, mocker):
    job_id = add_job(conn, add_sheet(conn))
    sup = make_supervisor()
    sup.tick(conn)
    db.mark_work_started(conn, job_id, sup.running.job["lease_token"])
    children[0].exit_code = 1

    get_job = mocker.patch("auto_archiver.job_queue.db.get_job", side_effect=OSError("Postgres has gone"))
    with pytest.raises(OSError):
        sup.tick(conn)
    assert sup.running is not None, "still remembers the job"

    mocker.stop(get_job)
    sup.tick(conn)
    assert job(conn, job_id)["status"] == "failed"


def test_files_and_processes_are_cleaned_after_an_exit(conn, make_supervisor, children, no_docker, tmp_path):
    job_id = add_job(conn, add_sheet(conn))
    sup = make_supervisor()
    sup.tick(conn)
    worker_dir = sup.dir
    os.makedirs(os.path.join(worker_dir, "tmp", "leftover"))
    os.makedirs(os.path.join(worker_dir, "logs"))
    children[0].exit_code = 1
    sup.tick(conn)
    assert not os.path.exists(os.path.join(worker_dir, "tmp"))
    assert os.path.exists(os.path.join(worker_dir, "logs")), "logs are kept"
    sup.kill.assert_called_with(worker_dir)
    no_docker.assert_any_call("rm", "-f", f"aa-job-{job_id}")


def test_startup_only_removes_the_queues_own_containers(make_supervisor, no_docker):
    no_docker.return_value = "abc123\n"
    make_supervisor().clear_leftovers()
    no_docker.assert_any_call("ps", "-aq", "--filter", "name=^aa-job-")
    no_docker.assert_any_call("rm", "-f", "abc123")


def test_old_container_is_removed_before_a_retry_starts(conn, make_supervisor, no_docker):
    job_id = add_job(conn, add_sheet(conn))
    make_supervisor().tick(conn)
    no_docker.assert_called_with("rm", "-f", f"aa-job-{job_id}")


def test_child_that_cannot_start_gives_the_job_back(conn, make_supervisor):
    job_id = add_job(conn, add_sheet(conn))

    def start(job, worker_dir):
        raise OSError("can't start")

    sup = make_supervisor(start=start)
    with pytest.raises(OSError):
        sup.tick(conn)
    current = job(conn, job_id)
    assert (current["status"], current["attempts"], current["lease_token"]) == ("queued", 0, None)
    assert sup.running is None and sup.is_idle()


def test_bad_reboot_time_stops_at_startup(monkeypatch):
    monkeypatch.setattr("sys.argv", ["worker_supervisor", "--reboot-at", "5:2x"])
    with pytest.raises(SystemExit):
        worker_supervisor.main()


def test_reboot_time_accepts_a_good_time():
    assert worker_supervisor.reboot_time("05:20") == "05:20"


# the time limit


def test_time_limit_kills_and_is_a_final_failure_with_no_retry(conn, make_supervisor, clock, children):
    job_id = add_job(conn, add_sheet(conn))
    sup = make_supervisor()
    sup.tick(conn)
    db.mark_work_started(conn, job_id, sup.running.job["lease_token"])

    clock.now += worker_supervisor.TIME_LIMIT_SECONDS - 1
    sup.check_time_limits()
    assert children[0].killed is False

    clock.now += 2
    sup.check_time_limits()
    assert children[0].killed is True
    sup.tick(conn)

    current = job(conn, job_id)
    assert current["status"] == "failed"
    assert "killed" in last_cells(conn, job_id)["status"]
    assert db.requeue_expired(conn) == (0, 0)


def test_time_limit_works_while_postgres_is_down_and_saves_when_it_returns(conn, make_supervisor, clock, children):
    job_id = add_job(conn, add_sheet(conn))
    sup = make_supervisor()
    sup.tick(conn)
    db.mark_work_started(conn, job_id, sup.running.job["lease_token"])
    clock.now += worker_supervisor.TIME_LIMIT_SECONDS + 1
    sup.check_time_limits()  # no connection needed
    assert children[0].killed is True
    assert job(conn, job_id)["status"] == "running"
    sup.tick(conn)
    assert job(conn, job_id)["status"] == "failed"


def test_time_limit_applies_to_a_child_killed_during_setup_too(conn, make_supervisor, clock):
    job_id = add_job(conn, add_sheet(conn))
    sup = make_supervisor()
    sup.tick(conn)
    clock.now += worker_supervisor.TIME_LIMIT_SECONDS + 1
    sup.check_time_limits()
    sup.tick(conn)
    assert job(conn, job_id)["status"] == "failed", "hanging for 3 hours isn't a setup failure to retry"


# repeated quick failures


def quick_setup_failure(conn, sup, children, sheet, row):
    add_job(conn, sheet, row)
    sup.tick(conn)
    children[-1].exit_code = 1
    sup.tick(conn)


def test_three_quick_failures_across_sheets_pause_the_worker_with_one_alert(conn, make_supervisor, clock, children):
    sheets = [add_sheet(conn, gid) for gid in (1, 2, 3)]
    sup = make_supervisor()
    for number, sheet in enumerate(sheets):
        quick_setup_failure(conn, sup, children, sheet, number + 2)
    assert alert_kinds(conn) == ["worker_paused"]

    add_job(conn, sheets[0], 10)
    started = len(children)
    sup.tick(conn)
    assert len(children) == started, "paused"

    clock.now += worker_supervisor.PAUSE_SECONDS + 1
    sup.tick(conn)
    assert len(children) == started + 1

    # still broken: pauses again with no new alert
    children[-1].exit_code = 1
    sup.tick(conn)
    for number, sheet in enumerate(sheets[1:]):
        quick_setup_failure(conn, sup, children, sheet, number + 20)
    assert sup.paused_until > clock.now
    assert alert_kinds(conn) == ["worker_paused"]


def test_one_sheets_failures_pause_the_sheet_not_the_worker(conn, make_supervisor, clock, children):
    sheet = add_sheet(conn)
    other = add_sheet(conn, 2)
    sup = make_supervisor()
    for row in (2, 3, 4):
        quick_setup_failure(conn, sup, children, sheet, row)
    assert alert_kinds(conn) == ["sheet_paused"]
    assert sup.paused_until == 0

    other_job = add_job(conn, other, 2)
    add_job(conn, sheet, 5)
    sup.tick(conn)
    assert sup.running.job["id"] == other_job, "the worker carries on with other sheets"


def test_a_row_failing_slowly_is_not_a_quick_failure(conn, make_supervisor, clock, children):
    sheets = [add_sheet(conn, gid) for gid in (1, 2, 3)]
    sup = make_supervisor()
    for number, sheet in enumerate(sheets):
        job_id = add_job(conn, sheet, number + 2)
        sup.tick(conn)
        db.mark_work_started(conn, job_id, sup.running.job["lease_token"])
        clock.now += worker_supervisor.QUICK_FAILURE_SECONDS + 1
        children[-1].exit_code = 1
        sup.tick(conn)
    assert sup.paused_until == 0


def test_rows_failing_quickly_after_setup_count(conn, make_supervisor, children):
    sheets = [add_sheet(conn, gid) for gid in (1, 2, 3)]
    sup = make_supervisor()
    for number, sheet in enumerate(sheets):
        job_id = add_job(conn, sheet, number + 2)
        sup.tick(conn)
        db.mark_work_started(conn, job_id, sup.running.job["lease_token"])
        children[-1].exit_code = 1
        sup.tick(conn)
    assert sup.paused_until > 0


def test_a_finished_job_clears_the_failures_and_the_alert(conn, make_supervisor, children):
    sheets = [add_sheet(conn, gid) for gid in (1, 2)]
    sup = make_supervisor()
    quick_setup_failure(conn, sup, children, sheets[0], 2)
    quick_setup_failure(conn, sup, children, sheets[1], 2)
    sup.pause_alerted = True

    job_id = add_job(conn, sheets[0], 3)
    sup.tick(conn)
    db.finish_job(conn, job_id, sup.running.job["lease_token"], "done", {"status": "ok"})
    children[-1].exit_code = 0
    sup.tick(conn)

    assert sup.quick_failures == []
    assert sup.pause_alerted is False


# draining and rebooting


def test_draining_lets_jobs_finish_and_claims_nothing_new(conn, make_supervisor, children):
    sheet = add_sheet(conn)
    first = add_job(conn, sheet, 2)
    add_job(conn, sheet, 3)
    sup = make_supervisor()
    sup.tick(conn)
    sup.draining = True
    sup.tick(conn)
    assert children[0].killed is False
    db.finish_job(conn, first, sup.running.job["lease_token"], "done", {"status": "ok"})
    children[0].exit_code = 0
    sup.tick(conn)
    assert len(children) == 1
    assert sup.is_idle()


def test_draining_logs_each_running_job(conn, make_supervisor, caplog):
    sheet = add_sheet(conn)
    conn.execute("UPDATE sheets SET spreadsheet_name = 'Example Sheet'")
    job_id = db.add_job(conn, sheet, 7, "https://example.com/post/7", "example.com", "folder1")
    conn.execute("UPDATE jobs SET available_at = now() - interval '1 minute'")
    sup = make_supervisor()
    sup.tick(conn)
    caplog.clear()
    sup.log_draining()
    expected = (
        f"Finishing job {job_id} Sheet 'Example Sheet' tab tab1 row 7 folder 'folder1' https://example.com/post/7"
    )
    assert expected in caplog.text


UK_MORNING = datetime(2026, 11, 2, 5, 30, tzinfo=timezone.utc)  # 05:30 in the UK in winter
BOOTED_YESTERDAY = UK_MORNING - timedelta(days=1)


def rebooting_supervisor(make_supervisor, mocker, now=UK_MORNING, booted=BOOTED_YESTERDAY, works=True):
    reboot = mocker.MagicMock(return_value=works)
    sup = make_supervisor(reboot_at="05:20", reboot=reboot, get_boot_time=lambda: booted, wall_clock=lambda: now)
    return sup, reboot


def test_no_reboot_time_never_reboots(conn, make_supervisor, mocker):
    reboot = mocker.MagicMock()
    sup = make_supervisor(reboot=reboot, get_boot_time=lambda: BOOTED_YESTERDAY, wall_clock=lambda: UK_MORNING)
    sup.tick(conn)
    reboot.assert_not_called()


def test_reboots_when_idle_after_the_time(conn, make_supervisor, mocker):
    sup, reboot = rebooting_supervisor(make_supervisor, mocker)
    sup.tick(conn)
    reboot.assert_called_once()


def test_no_reboot_before_the_time(conn, make_supervisor, mocker):
    sup, reboot = rebooting_supervisor(make_supervisor, mocker, now=UK_MORNING - timedelta(minutes=30))
    sup.tick(conn)
    reboot.assert_not_called()


def test_only_reboots_once_a_day(conn, make_supervisor, mocker):
    sup, reboot = rebooting_supervisor(make_supervisor, mocker, booted=UK_MORNING - timedelta(minutes=5))
    sup.tick(conn)
    reboot.assert_not_called()


def test_reboot_time_is_uk_time_in_summer_too(conn, make_supervisor, mocker):
    # 04:30 UTC is 05:30 in the UK in summer
    now = datetime(2026, 7, 1, 4, 30, tzinfo=timezone.utc)
    sup, reboot = rebooting_supervisor(make_supervisor, mocker, now=now, booted=now - timedelta(days=1))
    sup.tick(conn)
    reboot.assert_called_once()


def test_reboot_waits_for_running_jobs_and_claims_nothing_new(conn, make_supervisor, children, mocker):
    sheet = add_sheet(conn)
    add_job(conn, sheet, 2)
    plain = make_supervisor()
    plain.tick(conn)
    sup, reboot = rebooting_supervisor(make_supervisor, mocker)
    sup.running = plain.running
    add_job(conn, sheet, 3)
    sup.tick(conn)
    reboot.assert_not_called()
    assert len(children) == 1


def test_reboot_is_forced_an_hour_later(conn, make_supervisor, mocker):
    add_job(conn, add_sheet(conn), 2)
    plain = make_supervisor()
    plain.tick(conn)
    later = UK_MORNING + timedelta(minutes=55)
    sup, reboot = rebooting_supervisor(make_supervisor, mocker, now=later)
    sup.running = plain.running
    sup.tick(conn)
    reboot.assert_called_once()


def test_failed_reboot_alerts_once_and_carries_on_archiving(conn, make_supervisor, children, mocker):
    sup, reboot = rebooting_supervisor(make_supervisor, mocker, works=False)
    add_job(conn, add_sheet(conn))
    sup.tick(conn)
    sup.tick(conn)
    reboot.assert_called_once()
    assert alert_kinds(conn) == ["reboot_failed"]
    assert len(children) == 1


# real processes


def test_kill_children_kills_a_child_and_its_grandchild_but_not_others(tmp_path):
    grandchild_pid_file = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)\n"
        f"open({str(grandchild_pid_file)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    worker_dir, other_dir = str(tmp_path / "1"), str(tmp_path / "2")
    child = subprocess.Popen([sys.executable, "-c", script], env={**os.environ, "AA_WORKER_DIR": worker_dir})
    other = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], env={**os.environ, "AA_WORKER_DIR": other_dir}
    )
    grandchild_pid = None
    try:
        for _ in range(100):
            if grandchild_pid_file.exists() and grandchild_pid_file.read_text():
                break
            time.sleep(0.05)
        grandchild_pid = int(grandchild_pid_file.read_text())

        worker_supervisor.kill_children(worker_dir)

        assert child.wait(timeout=5) == -signal.SIGKILL
        for _ in range(100):
            if grandchild_pid not in worker_supervisor.child_pids(worker_dir):
                break
            time.sleep(0.05)
        assert worker_supervisor.child_pids(worker_dir) == []
        assert other.poll() is None, "another folder's job is untouched"
    finally:
        other.kill()
        child.kill()
        other.wait()
        child.wait()


def test_git_commit_shows_local_changes(mocker):
    def fake_run(args, **kwargs):
        output = {"rev-parse": "abc1234\n", "status": " M src/file.py\n"}[args[1]]
        return mocker.Mock(stdout=output)

    mocker.patch("auto_archiver.job_queue.worker_supervisor.subprocess.run", side_effect=fake_run)
    assert worker_supervisor.git_commit() == "abc1234+changes"


def test_git_commit_without_changes(mocker):
    def fake_run(args, **kwargs):
        return mocker.Mock(stdout={"rev-parse": "abc1234\n", "status": ""}[args[1]])

    mocker.patch("auto_archiver.job_queue.worker_supervisor.subprocess.run", side_effect=fake_run)
    assert worker_supervisor.git_commit() == "abc1234"
