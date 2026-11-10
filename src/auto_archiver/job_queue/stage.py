"""
Tells the dashboard what a queue job's archiver is doing now, and records how long each step takes in job_stages.
"""

import os

import psycopg
from psycopg.rows import dict_row

from auto_archiver.job_queue import db
from auto_archiver.utils.custom_logger import logger

_step = None  # this process's current step. Each archiver process runs one attempt of one job


def set_stage(step: str, progress: str = "") -> None:
    """
    step is eg an extractor's name or 'storing', and progress eg 'media 51 of 300'. A new step starts a job_stages row;
    the same step again only updates the progress. Does nothing outside a queue job (the supervisor sets AA_JOB_ID), so
    cron and local runs are unaffected. It's only for monitoring, so any error is logged rather than stopping the
    archive
    """
    global _step
    job_id = os.environ.get("AA_JOB_ID")
    if not job_id:
        return
    stage = f"{step}: {progress}" if progress else step
    try:
        with psycopg.connect(
            db.database_url(), autocommit=True, row_factory=dict_row, connect_timeout=5
        ) as conn:
            if step == _step:
                db.set_stage_progress(conn, int(job_id), stage)
            else:
                db.start_stage(conn, int(job_id), step, stage)
                _step = step
    except Exception as e:  # any bug here too, as a failed archive is worse than a missing stage
        logger.warning(f"Couldn't save the stage '{stage}' for the dashboard: {e}")
