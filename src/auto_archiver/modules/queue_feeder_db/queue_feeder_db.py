"""
Feeds the one job the supervisor claimed to the orchestrator, and saves its result to Postgres.

Opens a connection for each write rather than holding one for a row that can take hours, and retries a failed
connection, as the orchestrator swallows errors from done() and the archive would otherwise be lost.
"""

import time
from datetime import datetime, timezone
from typing import Iterator

import psycopg

from auto_archiver.core import Database, Feeder, Metadata
from auto_archiver.job_queue import db
from auto_archiver.modules.gsheet_feeder_db.gsheet_feeder_db import build_result_cells
from auto_archiver.utils.custom_logger import logger

# about 5 minutes, long enough for a Postgres restart
RETRY_DELAYS_SECONDS = (5, 15, 30, 60, 60, 60, 60)


class QueueFeederDB(Feeder, Database):
    def _with_database(self, action):
        """Runs action(conn) with a new connection, retrying connection failures"""
        for delay in (*RETRY_DELAYS_SECONDS, None):
            try:
                with db.connect() as conn:
                    return action(conn)
            except psycopg.OperationalError as e:
                if delay is None:
                    raise
                logger.warning(f"Postgres is unreachable, trying again in {delay}s: {e}")
                time.sleep(delay)

    def __iter__(self) -> Iterator[Metadata]:
        job = self._with_database(lambda conn: db.get_job(conn, self.job_id))
        if job is None or str(job["lease_token"]) != str(self.lease_token) or job["status"] != "running":
            logger.warning(f"Job {self.job_id} is no longer ours, not archiving it")
            return
        # setup worked: from here a failure is the row's, not the config's or VM's
        if not self._with_database(lambda conn: db.mark_work_started(conn, self.job_id, self.lease_token)):
            logger.warning(f"Job {self.job_id} is no longer ours, not archiving it")
            return

        self.row_description = f"row {job['row_number']} on {job['spreadsheet_id']} : {job['worksheet']}"
        item = Metadata().set_url(job["url"])
        if job["folder"]:
            item.set_context("folder", job["folder"])
        with logger.contextualize(worksheet=f"{job['spreadsheet_id']}:{job['worksheet']}", row=job["row_number"]):
            yield item

    def started(self, item: Metadata) -> None:
        # writes nothing, as the claim already added the status. The timings use this line
        logger.info("STARTED")
        logger.info(f" {self.row_description}")

    def _finish(self, status: str, cells: dict, error: str = None) -> None:
        saved = self._with_database(
            lambda conn: db.finish_job(conn, self.job_id, self.lease_token, status, cells, error)
        )
        if not saved:
            logger.warning(f"Job {self.job_id} is no longer ours, so this result wasn't saved")

    def failed(self, item: Metadata, reason: str) -> None:
        logger.error("FAILED")
        cells = {"status": f"Archive failed {reason}", "date": datetime.now(timezone.utc).isoformat()}
        self._finish("failed", cells, reason)

    def aborted(self, item: Metadata) -> None:
        logger.warning("ABORTED")

    def fetch(self, item: Metadata) -> bool:
        return False

    def done(self, item: Metadata, cached: bool = False) -> None:
        # the timings use this line, and the row line matches the feeder's
        logger.success(f"DONE {item.get_url()}")
        logger.success(f" {self.row_description}")
        if failures := item.get_store_failures():
            logger.warning(f"{len(failures)} file(s) couldn't be stored: {', '.join(failures)}")
        self._finish("done", build_result_cells(item, cached))
