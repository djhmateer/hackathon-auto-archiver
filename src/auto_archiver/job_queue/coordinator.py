"""
Coordinator: runs the sheet sync, sheet writer, cleanup and alert emails in turn, on one thread. See queue-spec.md

Each step has its own try/except, and Sheets calls have timeouts, so one failing or slow step can't stop the others.

    python -m auto_archiver.job_queue.coordinator
"""

import sys
import time
from dataclasses import dataclass, field

import psycopg

from auto_archiver.job_queue import alerts, db, migrate, sync, writer
from auto_archiver.utils.custom_logger import logger

# an idle writer doesn't call Google, so this only costs local SQL
CYCLE_SECONDS = 5
# sync reads every sheet, so it runs less often. 15 is for testing: go back to 60 before moving the busy sheets
SYNC_SECONDS = 15
# a worker beats every 2s, so this many seconds without one means it has stopped or lost Postgres
WORKER_QUIET_SECONDS = 30


def run_step(name: str, step) -> None:
    try:
        step()
    except Exception as e:
        logger.error(f"Coordinator step {name} failed: {e}")


def cleanup(conn) -> None:
    requeued, failed = db.requeue_expired(conn)
    if requeued or failed:
        logger.warning(f"Cleanup requeued {requeued} job(s) with an expired lease and failed {failed}")


def describe_job(job: dict) -> str:
    config = db.describe_config(job["config"])
    return (
        f"job {job['id']} {db.describe_tab(job['spreadsheet_name'], job['worksheet'])} ({config}) "
        f"row {job['row_number']} folder '{job['folder']}' {job['url']}"
    )


@dataclass
class Watcher:
    """Logs what changed since the last cycle, so the coordinator's output shows what the workers are doing"""

    started: bool = False
    workers_up: set = field(default_factory=set)
    last_job_id: int = 0
    running: dict = field(default_factory=dict)  # job id -> job, as last seen running
    waiting_for_site: dict = field(default_factory=dict)  # queued job id -> the running job id it's waiting for

    def watch(self, conn) -> None:
        if not self.started:
            # only log what happens from now on, not every job there has ever been
            jobs = db.get_jobs_after(conn, 0)
            self.last_job_id = jobs[-1]["id"] if jobs else 0
            self.running = {job["id"]: job for job in db.get_running_jobs(conn)}
            self.started = True
        self._watch_workers(conn)
        self._watch_jobs(conn)

    def _watch_workers(self, conn) -> None:
        for beat in db.get_worker_heartbeats(conn):
            worker, seconds_ago = beat["worker"], beat["seconds_ago"]
            if seconds_ago < WORKER_QUIET_SECONDS and worker not in self.workers_up:
                logger.info(f"Worker {worker} is up")
                self.workers_up.add(worker)
            elif seconds_ago >= WORKER_QUIET_SECONDS and worker in self.workers_up:
                logger.warning(f"Worker {worker} has gone quiet: no heartbeat for {seconds_ago}s")
                self.workers_up.discard(worker)

    def _watch_jobs(self, conn) -> None:
        for job in db.get_jobs_after(conn, self.last_job_id):
            if job["status"] == "rejected":
                logger.info(f"Rejected {describe_job(job)}: {job['error']}")
            else:
                logger.info(f"Queued {describe_job(job)}")
            self.last_job_id = job["id"]

        running = {job["id"]: job for job in db.get_running_jobs(conn)}
        for job_id, job in running.items():
            if job_id not in self.running:
                # started_at is the first claim, so this is only the wait for the first attempt
                waited = int((job["started_at"] - job["added_at"]).total_seconds())
                logger.info(
                    f"Worker {job['worker']} claimed {describe_job(job)}, attempt {job['attempts']}"
                    + (f", waited {waited}s in the queue" if job["attempts"] == 1 else "")
                )
        for job_id, job in self.running.items():
            if job_id not in running:
                current = db.get_job(conn, job_id)
                status = current["status"] if current else "gone"
                detail = f": {current['error']}" if current and current["error"] else ""
                if status == "done":
                    cells = db.get_last_cells(conn, job_id)
                    detail = f": archive {cells.get('archive', '')}, title {cells.get('title', '')!r}"
                log = logger.info if status == "done" else logger.warning
                log(f"Job {job_id} on worker {job['worker']} is now {status}{detail}")
                if status == "done" and current["domain"] == "vimeo.com" and current["extractor"] == "wacz":
                    logger.warning(
                        f"Job {job_id} got no video from vimeo.com, only a WACZ of the page. Cloudflare may be "
                        "challenging the workers' IP: open a Vimeo video in a browser on that network, pass the "
                        "check, then rerun the row"
                    )
        self.running = running

        # logged once per job, and again only if it ends up waiting for a different job
        waiting = {}
        for job in db.get_jobs_waiting_for_site(conn):
            waiting[job["id"]] = job["busy_job_id"]
            if self.waiting_for_site.get(job["id"]) != job["busy_job_id"]:
                logger.info(
                    f"Not claimed yet: {describe_job(job)} waits for its site {job['domain']}, "
                    f"as job {job['busy_job_id']} on worker {job['busy_worker']} is running it"
                )
        self.waiting_for_site = waiting


def cycle(conn, sync_due: bool, watcher: Watcher = None) -> None:
    def watch_and_write() -> None:
        # the watcher first, so a job's "Queued" and "claimed" lines come before the writer's lines for it
        if watcher is not None:
            run_step("watch", lambda: watcher.watch(conn))
        run_step("writer", lambda: writer.writer_cycle(conn))

    if sync_due:
        # DM 10th Nov 26 - also between spreadsheets: a pass over them all takes ~50s, which held up the sheet writes
        run_step("sync", lambda: sync.sync_cycle(conn, after_each=watch_and_write))
    watch_and_write()
    run_step("cleanup", lambda: cleanup(conn))
    run_step("alerts", lambda: alerts.email_alerts(conn))
    db.beat(conn, "coordinator")


def migrations_ok(conn) -> bool:
    pending, problems = migrate.check(conn)
    for problem in problems:
        logger.error(f"Migrations: {problem}")
    if pending:
        logger.error(f"Migrations not applied: {', '.join(pending)}. Run: python -m auto_archiver.job_queue.migrate")
    if not pending and not problems:
        logger.info("Migrations are up to date")
    return not pending and not problems


def log_startup(conn) -> None:
    logger.info(
        f"Coordinator starting: database {conn.info.dbname} on {conn.info.host}:{conn.info.port}, "
        f"writer and cleanup every {CYCLE_SECONDS}s, sync every {SYNC_SECONDS}s"
    )
    sheets = db.get_sheets(conn)
    if not sheets:
        logger.warning("No tabs registered, so there's nothing to do. Run: sync register <config>")
    for sheet in sheets:
        paused = f", paused until {sheet['paused_until']}" if sheet["paused_until"] else ""
        tab = db.describe_tab(sheet["spreadsheet_name"], sheet["worksheet"])  # "tab ..." when the name isn't known yet
        logger.info(
            f"{tab[0].upper()}{tab[1:]} "
            f"(sheet {sheet['id']}, {'live' if sheet['enabled'] else 'off'}{paused}): "
            f"spreadsheet {sheet['spreadsheet_id']}, config {sheet['config']}"
        )
    beats = db.get_worker_heartbeats(conn)
    if not beats:
        logger.info("No worker has ever sent a heartbeat")
    for beat in beats:
        logger.info(f"Worker {beat['worker']}: last heartbeat {beat['seconds_ago']}s ago")
    summary = db.get_queue_summary(conn)
    logger.info(
        f"Queue: {summary['queued']} queued, {summary['running']} running, {summary['unwritten']} sheet writes to do, "
        f"{summary['unsent_alerts']} unsent alerts"
    )


def main() -> None:
    try:
        with db.connect() as conn:
            # first, as the rest needs the right tables
            if not migrations_ok(conn):
                sys.exit(1)
            log_startup(conn)
    except psycopg.Error as e:
        logger.error(f"Coordinator can't start, as it can't read the database: {e}")
        sys.exit(1)

    last_sync = 0
    watcher = Watcher()
    while True:
        try:
            # a new connection each time, so a Postgres restart only loses one cycle
            with db.connect() as conn:
                sync_due = time.monotonic() - last_sync >= SYNC_SECONDS
                cycle(conn, sync_due, watcher)
                if sync_due:
                    last_sync = time.monotonic()
        except Exception as e:
            logger.error(f"Coordinator cycle failed: {e}")
        time.sleep(CYCLE_SECONDS)


if __name__ == "__main__":
    main()
