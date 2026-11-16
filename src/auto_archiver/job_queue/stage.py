"""
Tells the dashboard what a queue job's archiver is doing now, and records how long each step takes in job_stages,
and what it achieved.
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
        with psycopg.connect(db.database_url(), autocommit=True, row_factory=dict_row, connect_timeout=5) as conn:
            if step == _step:
                db.set_stage_progress(conn, int(job_id), stage)
            else:
                db.start_stage(conn, int(job_id), step, stage)
                _step = step
    except Exception as e:  # any bug here too, as a failed archive is worse than a missing stage
        logger.warning(f"Couldn't save the stage '{stage}' for the dashboard: {e}")


def step_result(worked: bool, new_notes: list[str], failure: str = "") -> tuple[str, str]:
    """
    A step's (result, detail) for job_stages, from whether it worked and the status notes it added, which are the
    same wording as the sheet's status text. failure is the detail when it didn't work and added no note
    """
    if any(note.startswith(("blocked by", "screenshot blocked")) or note == "aa IP blocked" for note in new_notes):
        result = "blocked"
    elif not worked:
        result = "failed"
    elif new_notes:
        result = "partial"
    else:
        result = "success"
    return result, "; ".join(new_notes) or ("" if worked else failure)


def set_result(result: str, detail: str = "") -> None:
    """Saves what the current step achieved. Like set_stage, does nothing outside a queue job and never raises"""
    job_id = os.environ.get("AA_JOB_ID")
    if not job_id or not _step:
        return
    try:
        with psycopg.connect(db.database_url(), autocommit=True, row_factory=dict_row, connect_timeout=5) as conn:
            db.set_stage_result(conn, int(job_id), _step, result, detail)
    except Exception as e:  # any bug here too, as a failed archive is worse than a missing result
        logger.warning(f"Couldn't save the result '{result}' of the stage '{_step}' for the dashboard: {e}")
