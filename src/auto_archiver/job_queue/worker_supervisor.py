"""
Supervisor: one per worker VM. It claims one job at a time and runs the archiver as a child process. See queue-spec.md

It checks the job every few seconds. Nothing here needs Postgres to keep a job running: if it's unreachable, the job
carries on and its result waits for it to come back.

    python -m auto_archiver.job_queue.worker_supervisor [--reboot-at 05:20]
"""

import argparse
import ctypes
import glob
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from auto_archiver.job_queue import db
from auto_archiver.utils.custom_logger import logger

TICK_SECONDS = 5
RENEW_SECONDS = 60
# the child's ROW_KILL_SECONDS (3 h) goes first, as it can report the failure. This is the backstop
TIME_LIMIT_SECONDS = 3 * 60 * 60 + 5 * 60
QUICK_FAILURE_SECONDS = 60
QUICK_FAILURES_TO_PAUSE = 3
PAUSE_SECONDS = 15 * 60
FORCE_REBOOT_AFTER_SECONDS = 60 * 60
REBOOT_TIMEZONE = ZoneInfo("Europe/London")
ARCHIVER_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def run_docker(*args: str) -> str:
    """Docker's output, or "" if it isn't installed or fails, as there's often nothing to remove"""
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug(f"docker {args[0]} failed: {e}")
        return ""


def child_pids(worker_dir: str) -> list[int]:
    """
    Every process started for a job, found by the AA_WORKER_DIR they inherit. Browsers can leave their process group,
    and a child that outlived the supervisor has no parent to follow, but they all keep this
    """
    marker = f"AA_WORKER_DIR={worker_dir}".encode()
    pids = []
    for environ in glob.glob("/proc/[0-9]*/environ"):
        try:
            with open(environ, "rb") as f:
                if marker in f.read().split(b"\0"):
                    pids.append(int(environ.split("/")[2]))
        except (OSError, ValueError):
            continue  # gone, or not ours
    return pids


def kill_children(worker_dir: str) -> None:
    for pid in child_pids(worker_dir):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass  # already gone


def _die_with_parent() -> None:
    """Runs in the child before it starts: it gets killed if the supervisor dies"""
    ctypes.CDLL(None).prctl(1, signal.SIGKILL)  # 1 is PR_SET_PDEATHSIG


def start_child(job: dict, worker_dir: str, extra_args: tuple = ()) -> subprocess.Popen:
    args = [
        sys.executable,
        # a native crash prints its stack to child.out, instead of nothing
        "-X",
        "faulthandler",
        ARCHIVER_PATH,
        "--config",
        job["config"],
        "--feeders",
        "queue_feeder_db",
        "--databases",
        "queue_feeder_db",
        f"--queue_feeder_db.job_id={job['id']}",
        f"--queue_feeder_db.lease_token={job['lease_token']}",
        # the child's own hang dump and kill timer only run with these. The orchestrator requires a file but doesn't
        # use it: it writes 1debug.log and the others to logs/
        "--logging.each_level_in_separate_file=true",
        f"--logging.file={os.path.join(worker_dir, 'logs', 'archiver.log')}",
        *extra_args,
    ]
    env = {**os.environ, "AA_WORKER_DIR": worker_dir, "AA_JOB_ID": str(job["id"])}
    # only the last job's, for setup errors which happen before the logs are set up
    os.makedirs(os.path.join(worker_dir, "logs"), exist_ok=True)
    with open(os.path.join(worker_dir, "logs", "child.out"), "w") as output:
        return subprocess.Popen(
            args, env=env, stdout=output, stderr=subprocess.STDOUT, preexec_fn=_die_with_parent, start_new_session=True
        )


def reboot_machine() -> bool:
    return subprocess.run(["sudo", "-n", "/sbin/reboot"]).returncode == 0


def boot_time() -> datetime:
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("btime"):
                return datetime.fromtimestamp(int(line.split()[1]), timezone.utc)
    raise RuntimeError("No boot time in /proc/stat")


def describe_job(job: dict) -> str:
    return f"job {job['id']} row {job['row_number']} folder '{job['folder']}' {job['url']}"


@dataclass
class RunningJob:
    job: dict
    process: object
    started: float
    renewed: float
    kill_reason: str = None  # the supervisor killed it for this, so it's a final failure
    stolen: bool = False  # another worker has the job, so its state isn't ours to touch


class WorkerSupervisor:
    def __init__(
        self,
        worker: str,
        reboot_at: str = None,
        start=start_child,
        kill=kill_children,
        reboot=reboot_machine,
        get_boot_time=boot_time,
        clock=time.monotonic,
        wall_clock=lambda: datetime.now(timezone.utc),
    ):
        self.worker = worker
        self.dir = os.getcwd()
        self.running = None
        self.reboot_at = reboot_at
        self.start = start
        self.kill = kill
        self.reboot = reboot
        self.get_boot_time = get_boot_time
        self.clock = clock
        self.wall_clock = wall_clock
        self.draining = False
        self.reboot_failed = False
        self.paused_until = 0
        self.pause_alerted = False
        self.quick_failures = []  # the sheet of each quick failure in a row

    def clear_leftovers(self) -> None:
        """At startup: a child and crawls from a supervisor that died. Its job comes back when the lease expires"""
        self.kill(self.dir)
        shutil.rmtree(os.path.join(self.dir, "tmp"), ignore_errors=True)
        for container in run_docker("ps", "-aq", "--filter", "name=^aa-job-").split():
            run_docker("rm", "-f", container)

    def is_idle(self) -> bool:
        return self.running is None

    def _kill_child(self) -> None:
        self.running.process.kill()
        self.kill(self.dir)
        run_docker("rm", "-f", f"aa-job-{self.running.job['id']}")

    def check_time_limits(self) -> None:
        """
        Needs no Postgres, so the limit holds during an outage. The job's failure is saved once the child's gone. If
        cleanup requeues it first, after a long outage, it gets one more attempt instead of failing. That's acceptable
        """
        running = self.running
        if (
            running is not None
            and running.kill_reason is None
            and self.clock() - running.started > TIME_LIMIT_SECONDS
        ):
            logger.error(f"Job {running.job['id']} hit the time limit, killing it")
            running.kill_reason = f"- still running after {TIME_LIMIT_SECONDS // 60} minutes, killed"
            self._kill_child()

    def _record_quick_failure(self, sheet_id: int, conn) -> None:
        """
        Otherwise a broken VM fails jobs in seconds, claims more than healthy VMs and empties the queue. Only across
        more than one sheet, as one sheet's bad config is handled by pausing that sheet
        """
        self.quick_failures.append(sheet_id)
        recent = self.quick_failures[-QUICK_FAILURES_TO_PAUSE:]
        if len(recent) == QUICK_FAILURES_TO_PAUSE and len(set(recent)) > 1:
            logger.error(
                f"{QUICK_FAILURES_TO_PAUSE} quick failures in a row, not claiming for {PAUSE_SECONDS // 60} min"
            )
            self.paused_until = self.clock() + PAUSE_SECONDS
            self.quick_failures = []
            if not self.pause_alerted:
                db.add_alert(conn, "worker_paused", f"Worker {self.worker} paused after repeated quick failures")
                self.pause_alerted = True

    def _child_exited(self, conn) -> None:
        """Works out what happened and saves it. If Postgres fails, the next tick does this again"""
        running = self.running
        job, ran_for = running.job, self.clock() - running.started
        current = None if running.stolen else db.get_job(conn, job["id"])
        ours = current is not None and current["lease_token"] == job["lease_token"]

        if running.stolen or not ours:
            logger.warning(f"Job {job['id']} now belongs to another worker, leaving it alone")
        elif current["status"] == "done":
            self.quick_failures, self.pause_alerted = [], False
        elif current["status"] == "failed":
            self._record_failure(conn, job, ran_for)
        elif running.kill_reason:
            self._fail(conn, job, running.kill_reason)
            self.quick_failures = []
        elif current["work_started_at"] is None:
            # negative is the signal that killed it. It may have had no chance to log anything
            code = running.process.poll()
            logger.error(f"Job {job['id']} exited during setup with code {code}, see {self.dir}/logs/child.out")
            db.release_setup_failure(conn, job["id"], job["lease_token"])
            self._record_quick_failure(job["sheet_id"], conn)
        else:
            logger.error(f"Job {job['id']} exited without saving a result")
            self._fail(conn, job, "- the archiver stopped unexpectedly - please rerun")
            self._record_failure(conn, job, ran_for)

        self.kill(self.dir)
        run_docker("rm", "-f", f"aa-job-{job['id']}")
        shutil.rmtree(os.path.join(self.dir, "tmp"), ignore_errors=True)
        self.running = None

    def _record_failure(self, conn, job: dict, ran_for: float) -> None:
        if ran_for < QUICK_FAILURE_SECONDS:
            self._record_quick_failure(job["sheet_id"], conn)
        else:
            self.quick_failures = []

    def _fail(self, conn, job: dict, reason: str) -> None:
        cells = {"status": f"Archive failed {reason}", "date": datetime.now(timezone.utc).isoformat()}
        db.finish_job(conn, job["id"], job["lease_token"], "failed", cells, reason)

    def _check_running(self, conn) -> None:
        running = self.running
        if running.process.poll() is not None:
            self._child_exited(conn)
        elif not running.kill_reason and self.clock() - running.renewed >= RENEW_SECONDS:
            # a connection error raises and leaves the job running: only a changed token means it's been taken
            if db.renew_lease(conn, running.job["id"], running.job["lease_token"]):
                running.renewed = self.clock()
            else:
                logger.warning(f"Job {running.job['id']} was taken by another worker, killing the child")
                running.stolen = True
                running.kill_reason = "taken by another worker"
                self._kill_child()

    def _reboot_due(self) -> tuple[bool, bool]:
        """(due, forced), as the cron scripts did: the first idle moment after reboot_at, at most once a day"""
        if not self.reboot_at or self.reboot_failed:
            return False, False
        now = self.wall_clock()
        hour, minute = map(int, self.reboot_at.split(":"))
        target = now.astimezone(REBOOT_TIMEZONE).replace(hour=hour, minute=minute, second=0, microsecond=0)
        due = now >= target and self.get_boot_time() < target
        return due, due and (now - target).total_seconds() >= FORCE_REBOOT_AFTER_SECONDS

    def _reboot_if_due(self, conn) -> bool:
        """True if a reboot is due, so nothing new should be claimed"""
        due, forced = self._reboot_due()
        if not due:
            return False
        if self.is_idle() or forced:
            logger.info(f"Daily reboot{' FORCED' if forced and not self.is_idle() else ''}")
            if not self.reboot():
                # archiving matters more than the reboot
                logger.error("Daily reboot failed, carrying on archiving")
                self.reboot_failed = True
                db.add_alert(conn, "reboot_failed", f"Worker {self.worker} couldn't do its daily reboot")
                return False
        return True

    def _claim(self, conn) -> None:
        job = db.claim_job(conn, self.worker)
        if job is None:
            return
        run_docker("rm", "-f", f"aa-job-{job['id']}")  # a retry reuses the name
        try:
            process = self.start(job, self.dir)
        except Exception:
            # nothing is running it, so don't leave it waiting for its lease to expire
            db.release_setup_failure(conn, job["id"], job["lease_token"])
            raise
        self.running = RunningJob(job, process, self.clock(), self.clock())
        logger.info(f"Started {describe_job(job)}")

    def tick(self, conn) -> None:
        if self.running is not None:
            self._check_running(conn)
        rebooting = self._reboot_if_due(conn)
        if self.draining or rebooting or self.clock() < self.paused_until:
            return
        if self.running is None:
            self._claim(conn)

    def log_draining(self) -> None:
        logger.info("Finishing the running job, then stopping")
        if self.running is not None:
            logger.info(f"Finishing {describe_job(self.running.job)}")

    def run(self) -> None:
        self.clear_leftovers()

        def drain(signum, frame):
            self.draining = True  # logged in the loop, as loguru can raise if a signal interrupts a log call

        signal.signal(signal.SIGTERM, drain)
        signal.signal(signal.SIGINT, drain)

        drain_logged = False
        while not (self.draining and self.is_idle()):
            if self.draining and not drain_logged:
                self.log_draining()
                drain_logged = True
            self.check_time_limits()
            try:
                # a new connection each time, so a Postgres restart only loses a few ticks
                with db.connect() as conn:
                    self.tick(conn)
                    db.beat(conn, f"worker-supervisor-{self.worker}")
            except Exception as e:
                logger.error(f"Supervisor tick failed, jobs carry on running: {e}")
            time.sleep(TICK_SECONDS)
        logger.info("Stopped")


def reboot_time(value: str) -> str:
    """Checked at startup, as a bad value would stop every tick before it could claim"""
    datetime.strptime(value, "%H:%M")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Runs archiver jobs from the job queue")
    parser.add_argument("--worker", default=socket.gethostname(), help="this VM's name")
    parser.add_argument(
        "--reboot-at", type=reboot_time, help="UK time for the daily reboot, eg 05:20. No reboots if left out"
    )
    args = parser.parse_args()
    WorkerSupervisor(args.worker, reboot_at=args.reboot_at).run()


if __name__ == "__main__":
    main()
