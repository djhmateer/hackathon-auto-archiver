"""
Feeds the one job the supervisor claimed to the orchestrator, and saves its result to Postgres.

Opens a connection for each write rather than holding one for a row that can take hours, and retries a failed
connection, as the orchestrator swallows errors from done() and the archive would otherwise be lost.
"""

import hashlib
import os
import time
from datetime import datetime, timezone
from typing import Iterator

import psycopg

from auto_archiver.core import Database, Feeder, Metadata
from auto_archiver.job_queue import db
from auto_archiver.modules.gsheet_feeder_db.gsheet_feeder_db import build_result_cells
from auto_archiver.utils.custom_logger import logger
from auto_archiver.utils.misc import calculate_file_hash

# the hash enricher's algorithms, so a file it missed gets the same one
HASH_ALGORITHMS = {"SHA-256": hashlib.sha256, "SHA3-512": hashlib.sha3_512}
# types Python's mimetypes doesn't know
EXTRA_MIMETYPES = {".wacz": "application/wacz"}

# about 5 minutes, long enough for a Postgres restart
RETRY_DELAYS_SECONDS = (5, 15, 30, 60, 60, 60, 60)


def describe_result(item: Metadata, cells: dict, cached: bool) -> dict:
    """The job's columns for the dashboard, as db.RESULT_COLUMNS"""
    extractor = item.status.split(": success")[0] if ": success" in (item.status or "") else None
    return {
        "title": cells.get("title"),
        "archive_url": cells.get("archive"),
        "extractor": extractor,
        "posted_at": item.get_timestamp(iso=False),
        "cached": cached,
        "store_failures": len(item.get_store_failures()),
    }


def describe_files(item: Metadata) -> list[dict]:
    """
    Each stored file, as db.FILE_COLUMNS. Run in done(), while the files are still in the tmp dir. A file the hash
    enricher missed (eg a screenshot or WACZ, made after it) is hashed here, with the algorithm it used
    """
    all_media = item.get_all_media()
    used = {(m.get("hash") or "").split(":")[0] for m in all_media} & HASH_ALGORITHMS.keys()
    algorithm = "SHA3-512" if "SHA3-512" in used else "SHA-256"
    files = []
    for media in all_media:
        size, file_hash = None, media.get("hash")
        if media.filename and os.path.isfile(media.filename):
            size = os.path.getsize(media.filename)
            if not file_hash:
                file_hash = f"{algorithm}:{calculate_file_hash(media.filename, HASH_ALGORITHMS[algorithm])}"
        mimetype = None
        if media.filename:
            mimetype = media.mimetype or EXTRA_MIMETYPES.get(os.path.splitext(media.filename)[1].lower())
        try:
            duration = float(media.get("duration"))
        except (TypeError, ValueError):
            duration = None
        files.append(
            {
                "role": media.get("id") or "",
                "key": media.key,
                "url": media.urls[0] if media.urls else None,
                "mimetype": mimetype or None,
                "bytes": size,
                "hash": file_hash,
                "duration_seconds": duration,
            }
        )
    return files


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

    def _finish(self, status: str, cells: dict, error: str = None, result: dict = None, files: list = ()) -> None:
        saved = self._with_database(
            lambda conn: db.finish_job(conn, self.job_id, self.lease_token, status, cells, error, result, files)
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
        cells = build_result_cells(item, cached)
        try:
            result, files = describe_result(item, cells, cached), describe_files(item)
        except Exception as e:
            # the stats are extra: losing them mustn't lose the archive's result
            logger.error(f"Couldn't work out the archive's stats, saving the result without them: {e}")
            result, files = None, ()
        self._finish("done", cells, result=result, files=files)
