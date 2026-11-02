"""
Coordinator: runs the sheet sync, sheet writer and cleanup in turn, on one thread. See queue-spec.md

Each step has its own try/except, and Sheets calls have timeouts, so one failing or slow step can't stop the others.

    python -m auto_archiver.job_queue.coordinator
"""

import sys
import time

import psycopg

from auto_archiver.job_queue import db, migrate, sync, writer
from auto_archiver.utils.custom_logger import logger

# an idle writer doesn't call Google, so this only costs local SQL
CYCLE_SECONDS = 5
# sync reads every sheet, so it runs less often. 30 is for testing: go back to 60 before moving the busy sheets
SYNC_SECONDS = 30


def run_step(name: str, step) -> None:
    try:
        step()
    except Exception as e:
        logger.error(f"Coordinator step {name} failed: {e}")


def cleanup(conn) -> None:
    requeued, failed = db.requeue_expired(conn)
    if requeued or failed:
        logger.warning(f"Cleanup requeued {requeued} job(s) with an expired lease and failed {failed}")


def cycle(conn, sync_due: bool) -> None:
    if sync_due:
        run_step("sync", lambda: sync.sync_cycle(conn))
    run_step("writer", lambda: writer.writer_cycle(conn))
    run_step("cleanup", lambda: cleanup(conn))
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
        logger.info(
            f"Tab {sheet['worksheet']} (sheet {sheet['id']}, {'live' if sheet['enabled'] else 'shadow'}{paused}): "
            f"spreadsheet {sheet['spreadsheet_id']}, config {sheet['config']}"
        )
    summary = db.get_queue_summary(conn)
    logger.info(
        f"Queue: {summary['queued']} queued, {summary['running']} running, {summary['unwritten']} sheet writes to do, "
        f"{summary['conflicts']} conflicts, {summary['unsent_alerts']} unsent alerts"
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
    while True:
        try:
            # a new connection each time, so a Postgres restart only loses one cycle
            with db.connect() as conn:
                sync_due = time.monotonic() - last_sync >= SYNC_SECONDS
                cycle(conn, sync_due)
                if sync_due:
                    last_sync = time.monotonic()
        except Exception as e:
            logger.error(f"Coordinator cycle failed: {e}")
        time.sleep(CYCLE_SECONDS)


if __name__ == "__main__":
    main()
