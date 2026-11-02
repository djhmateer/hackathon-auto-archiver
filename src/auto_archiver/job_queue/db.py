"""
All the SQL for the job queue. See queue-spec.md

Only imports psycopg and the standard library, so the coordinator, supervisor and queue_feeder_db can all use it.
Every function takes an open connection from connect(), and anything that writes twice uses a transaction.
"""

from pathlib import Path
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

STATUS_QUEUED = "Queued"
STATUS_IN_PROGRESS = "Archive in progress"

ENV_FILE = Path(".env")  # in the repo root, which everything runs from
MAX_ATTEMPTS = 3
# setup failures in a row before a sheet is paused
MAX_SETUP_FAILURES = 3


def database_url(env_file: Path = ENV_FILE) -> str:
    """The AA_DATABASE_URL line of the .env file"""
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("AA_DATABASE_URL="):
                return line.split("=", 1)[1].strip().strip("'\"")
    raise RuntimeError(f"No database set: add AA_DATABASE_URL=<connection string> to {env_file.absolute()}")


def connect(url: str = None) -> psycopg.Connection:
    """autocommit, so a single statement commits by itself and several use `with conn.transaction()`"""
    return psycopg.connect(url or database_url(), autocommit=True, row_factory=dict_row)


def add_alert(conn: psycopg.Connection, kind: str, message: str) -> None:
    conn.execute("INSERT INTO alerts (kind, message) VALUES (%s, %s)", (kind, message))


def add_alert_once(conn: psycopg.Connection, kind: str, message: str) -> None:
    """For something checked every cycle: at most one alert a day for the same thing"""
    conn.execute(
        """
        INSERT INTO alerts (kind, message)
        SELECT %(kind)s, %(message)s
        WHERE NOT EXISTS (
            SELECT 1 FROM alerts WHERE kind = %(kind)s AND message = %(message)s
                AND created_at > now() - interval '1 day'
        )
        """,
        {"kind": kind, "message": message},
    )


def now(conn: psycopg.Connection):
    """The database's time, so all the comparisons use one clock"""
    return conn.execute("SELECT now() AS now").fetchone()["now"]


def beat(conn: psycopg.Connection, name: str) -> None:
    conn.execute(
        "INSERT INTO heartbeats (name) VALUES (%s) ON CONFLICT (name) DO UPDATE SET beat_at = now()",
        (name,),
    )


def save_tab(
    conn: psycopg.Connection, spreadsheet_id: str, gid: int, title: str, config: str, update_config: bool = False
) -> None:
    """
    Adds a tab, off so cron keeps it until it's moved. A tab added to a spreadsheet already on the queue starts on,
    as cron no longer reads that spreadsheet. A known tab only gets its current title, and its config when registering:
    sync can't change it, or a tab allowed by two configs would swap between them every cycle
    """
    conn.execute(
        """
        INSERT INTO sheets (spreadsheet_id, worksheet_gid, worksheet, config, enabled)
        VALUES (%(id)s, %(gid)s, %(title)s, %(config)s,
                EXISTS (SELECT 1 FROM sheets WHERE spreadsheet_id = %(id)s AND enabled))
        ON CONFLICT (spreadsheet_id, worksheet_gid) DO UPDATE SET worksheet = EXCLUDED.worksheet,
            config = CASE WHEN %(update_config)s THEN EXCLUDED.config ELSE sheets.config END
        """,
        {"id": spreadsheet_id, "gid": gid, "title": title, "config": config, "update_config": update_config},
    )


def enable_spreadsheet(conn: psycopg.Connection, spreadsheet_id: str) -> int:
    """Moves all of a spreadsheet's tabs to the queue. Returns how many tabs it has"""
    return conn.execute("UPDATE sheets SET enabled = true WHERE spreadsheet_id = %s", (spreadsheet_id,)).rowcount


def rollback_spreadsheet(conn: psycopg.Connection, spreadsheet_id: str) -> tuple[int, int, int]:
    """
    Takes a spreadsheet off the queue: turns it off, then cancels its waiting jobs, which clears their status. Safe to
    run again. Returns (cancelled, still running, sheet writes still to do): cron stays off until the last two are 0
    """
    conn.execute("UPDATE sheets SET enabled = false WHERE spreadsheet_id = %s", (spreadsheet_id,))
    queued = conn.execute(
        """
        SELECT j.id FROM jobs j JOIN sheets s ON s.id = j.sheet_id
        WHERE s.spreadsheet_id = %s AND j.status = 'queued' ORDER BY j.id
        """,
        (spreadsheet_id,),
    ).fetchall()
    cancelled = sum(cancel_job(conn, job["id"]) for job in queued)
    running = conn.execute(
        """
        SELECT count(*) AS n FROM jobs j JOIN sheets s ON s.id = j.sheet_id
        WHERE s.spreadsheet_id = %s AND j.status = 'running'
        """,
        (spreadsheet_id,),
    ).fetchone()["n"]
    unwritten = conn.execute(
        """
        SELECT count(*) AS n FROM outbox o JOIN jobs j ON j.id = o.job_id JOIN sheets s ON s.id = j.sheet_id
        WHERE s.spreadsheet_id = %s AND o.written_at IS NULL AND o.discarded_at IS NULL
        """,
        (spreadsheet_id,),
    ).fetchone()["n"]
    return cancelled, running, unwritten


def get_sheets(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("SELECT * FROM sheets ORDER BY id").fetchall()


def get_queue_summary(conn: psycopg.Connection) -> dict:
    return conn.execute(
        """
        SELECT
            (SELECT count(*) FROM jobs WHERE status = 'queued') AS queued,
            (SELECT count(*) FROM jobs WHERE status = 'running') AS running,
            (SELECT count(*) FROM outbox WHERE written_at IS NULL AND discarded_at IS NULL) AS unwritten,
            (SELECT count(*) FROM outbox WHERE conflict IS NOT NULL
                AND written_at IS NULL AND discarded_at IS NULL) AS conflicts,
            (SELECT count(*) FROM alerts WHERE sent_at IS NULL) AS unsent_alerts
        """
    ).fetchone()


def get_open_shadow_jobs(conn: psycopg.Connection, sheet_id: int) -> list[dict]:
    return conn.execute(
        "SELECT * FROM shadow_jobs WHERE sheet_id = %s AND closed_at IS NULL ORDER BY id", (sheet_id,)
    ).fetchall()


def add_shadow_job(conn: psycopg.Connection, sheet_id: int, row_number: int, url: str) -> None:
    conn.execute(
        """
        INSERT INTO shadow_jobs (sheet_id, row_number, url) VALUES (%s, %s, %s)
        ON CONFLICT (sheet_id, row_number) WHERE closed_at IS NULL DO NOTHING
        """,
        (sheet_id, row_number, url),
    )


def close_shadow_job(conn: psycopg.Connection, shadow_job_id: int) -> None:
    conn.execute("UPDATE shadow_jobs SET closed_at = now() WHERE id = %s", (shadow_job_id,))


def add_job(
    conn: psycopg.Connection, sheet_id: int, row_number: int, url: str, domain: str, folder: str = ""
) -> int | None:
    """Adds a job and its Queued status. Returns the job id, or None if the row already has an active job"""
    with conn.transaction():
        job = conn.execute(
            """
            INSERT INTO jobs (sheet_id, row_number, url, domain, folder)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (sheet_id, row_number) WHERE status IN ('queued', 'running') DO NOTHING
            RETURNING id
            """,
            (sheet_id, row_number, url, domain, folder),
        ).fetchone()
        if job is None:
            return None
        conn.execute(
            "INSERT INTO outbox (job_id, cells, expected_status) VALUES (%s, %s, '')",
            (job["id"], Jsonb({"status": STATUS_QUEUED})),
        )
    return job["id"]


def add_rejected_job(conn: psycopg.Connection, sheet_id: int, row_number: int, url: str, message: str) -> None:
    """A row that failed the checks: never runs, but every sheet write has a job"""
    with conn.transaction():
        job = conn.execute(
            """
            INSERT INTO jobs (sheet_id, row_number, url, domain, status, finished_at, error)
            VALUES (%s, %s, %s, '', 'rejected', now(), %s) RETURNING id
            """,
            (sheet_id, row_number, url, message),
        ).fetchone()
        conn.execute(
            "INSERT INTO outbox (job_id, cells, expected_status) VALUES (%s, %s, '')",
            (job["id"], Jsonb({"status": message})),
        )


def cancel_job(conn: psycopg.Connection, job_id: int) -> bool:
    """Cancels a queued job and clears its status. False if a worker claimed it first, so it carries on"""
    with conn.transaction():
        result = conn.execute(
            "UPDATE jobs SET status = 'cancelled', finished_at = now() WHERE id = %s AND status = 'queued'",
            (job_id,),
        )
        if result.rowcount != 1:
            return False
        # a job requeued after being claimed has the in progress status on its row by now
        claimed = conn.execute(
            "SELECT 1 FROM outbox WHERE job_id = %s AND expected_status = %s", (job_id, STATUS_QUEUED)
        ).fetchone()
        # skips the URL check, which fails by definition when the URL has changed
        conn.execute(
            "INSERT INTO outbox (job_id, cells, expected_status, skip_url_check) VALUES (%s, %s, %s, true)",
            (job_id, Jsonb({"status": ""}), STATUS_IN_PROGRESS if claimed else STATUS_QUEUED),
        )
    return True


def get_row_states(conn: psycopg.Connection, sheet_id: int, snapshot_at) -> list[dict]:
    """What sync needs to know about a sheet's rows. Rows with nothing active or recent aren't returned"""
    return conn.execute(
        """
        SELECT j.row_number,
            max(j.id) FILTER (WHERE j.status IN ('queued', 'running')) AS job_id,
            max(j.status) FILTER (WHERE j.status IN ('queued', 'running')) AS job_status,
            max(j.url) FILTER (WHERE j.status IN ('queued', 'running')) AS job_url,
            COALESCE(bool_or(o.id IS NOT NULL AND o.written_at IS NULL AND o.discarded_at IS NULL), false)
                AS has_unwritten,
            COALESCE(bool_or(o.written_at >= %(snapshot_at)s), false) AS written_since_snapshot
        FROM jobs j LEFT JOIN outbox o ON o.job_id = j.id
        WHERE j.sheet_id = %(sheet_id)s AND (
            j.status IN ('queued', 'running')
            OR (o.id IS NOT NULL AND o.written_at IS NULL AND o.discarded_at IS NULL)
            OR o.written_at >= %(snapshot_at)s
        )
        GROUP BY j.row_number
        """,
        {"sheet_id": sheet_id, "snapshot_at": snapshot_at},
    ).fetchall()


def get_next_outbox_entries(conn: psycopg.Connection) -> list[dict]:
    """The oldest unwritten entry of each job. A job whose oldest has a conflict waits, so updates stay in order"""
    return conn.execute(
        """
        SELECT * FROM (
            SELECT DISTINCT ON (o.job_id) o.id, o.job_id, o.cells, o.expected_status, o.skip_url_check, o.conflict,
                j.row_number, j.url, s.id AS sheet_id, s.spreadsheet_id, s.worksheet_gid, s.config
            FROM outbox o JOIN jobs j ON j.id = o.job_id JOIN sheets s ON s.id = j.sheet_id
            WHERE o.written_at IS NULL AND o.discarded_at IS NULL
            ORDER BY o.job_id, o.id
        ) next_entries
        WHERE conflict IS NULL
        ORDER BY id
        """
    ).fetchall()


def mark_written(conn: psycopg.Connection, outbox_ids: list[int]) -> None:
    """Only once the sheet has been updated, as sync rule 1 relies on that"""
    conn.execute("UPDATE outbox SET written_at = now() WHERE id = ANY(%s)", (outbox_ids,))


def set_conflict(conn: psycopg.Connection, outbox_id: int, conflict: str) -> None:
    with conn.transaction():
        conn.execute("UPDATE outbox SET conflict = %s WHERE id = %s", (conflict, outbox_id))
        add_alert(conn, "writer_conflict", f"Outbox entry {outbox_id}: {conflict}")


def get_conflicts(conn: psycopg.Connection) -> list[dict]:
    return conn.execute(
        """
        SELECT o.id, o.job_id, j.sheet_id, j.row_number, o.conflict, o.created_at
        FROM outbox o JOIN jobs j ON j.id = o.job_id
        WHERE o.conflict IS NOT NULL AND o.written_at IS NULL AND o.discarded_at IS NULL
        ORDER BY o.id
        """
    ).fetchall()


def retry_conflict(conn: psycopg.Connection, outbox_id: int) -> bool:
    result = conn.execute(
        "UPDATE outbox SET conflict = NULL WHERE id = %s AND conflict IS NOT NULL AND written_at IS NULL "
        "AND discarded_at IS NULL",
        (outbox_id,),
    )
    return result.rowcount == 1


def discard_conflict(conn: psycopg.Connection, outbox_id: int) -> bool:
    result = conn.execute(
        "UPDATE outbox SET discarded_at = now() WHERE id = %s AND conflict IS NOT NULL AND written_at IS NULL "
        "AND discarded_at IS NULL",
        (outbox_id,),
    )
    return result.rowcount == 1


def claim_job(conn: psycopg.Connection, worker: str) -> dict | None:
    """
    Claims the next job, or returns None if there isn't one. The sheet with the fewest running jobs for its weight
    goes first, then the oldest job.

    Only the job's first claim adds the in progress status. That's found from the outbox rather than attempts, because
    a setup failure gives its attempt back.
    """
    with conn.transaction():
        job = conn.execute(
            """
            UPDATE jobs SET status = 'running', worker = %s, attempts = attempts + 1,
                started_at = COALESCE(started_at, now()), work_started_at = NULL,
                lease_token = gen_random_uuid(), lease_until = now() + interval '5 minutes'
            WHERE id = (
                SELECT j.id FROM jobs j JOIN sheets s ON s.id = j.sheet_id
                WHERE j.status = 'queued' AND j.available_at <= now() AND s.enabled
                    AND (s.paused_until IS NULL OR s.paused_until <= now())
                ORDER BY (SELECT count(*) FROM jobs r WHERE r.sheet_id = j.sheet_id
                          AND r.status = 'running')::float / s.weight, j.id
                FOR UPDATE OF j SKIP LOCKED LIMIT 1
            ) RETURNING *, (SELECT config FROM sheets s WHERE s.id = jobs.sheet_id) AS config
            """,
            (worker,),
        ).fetchone()
        if job is None:
            return None
        conn.execute(
            """
            INSERT INTO outbox (job_id, cells, expected_status)
            SELECT %(id)s, %(cells)s, %(queued)s
            WHERE NOT EXISTS (SELECT 1 FROM outbox WHERE job_id = %(id)s AND expected_status = %(queued)s)
            """,
            {"id": job["id"], "cells": Jsonb({"status": STATUS_IN_PROGRESS}), "queued": STATUS_QUEUED},
        )
    return job


def get_job(conn: psycopg.Connection, job_id: int) -> dict | None:
    return conn.execute(
        """
        SELECT j.*, s.spreadsheet_id, s.worksheet, s.config
        FROM jobs j JOIN sheets s ON s.id = j.sheet_id
        WHERE j.id = %s
        """,
        (job_id,),
    ).fetchone()


def renew_lease(conn: psycopg.Connection, job_id: int, lease_token: str) -> bool:
    """
    False means another worker has the job, so kill the child. Doesn't check lease_until, so a supervisor can still
    renew after a Postgres restart if cleanup hasn't taken the job. Connection errors are left to raise
    """
    result = conn.execute(
        """
        UPDATE jobs SET lease_until = now() + interval '5 minutes'
        WHERE id = %s AND lease_token = %s AND status = 'running'
        """,
        (job_id, lease_token),
    )
    return result.rowcount == 1


def mark_work_started(conn: psycopg.Connection, job_id: int, lease_token: str) -> bool:
    """This attempt got past setup, which also proves the sheet's config works"""
    with conn.transaction():
        job = conn.execute(
            """
            UPDATE jobs SET work_started_at = now()
            WHERE id = %s AND lease_token = %s AND status = 'running'
            RETURNING sheet_id
            """,
            (job_id, lease_token),
        ).fetchone()
        if job is None:
            return False
        conn.execute(
            "UPDATE sheets SET setup_failures = 0, paused_until = NULL WHERE id = %s",
            (job["sheet_id"],),
        )
    return True


def finish_job(
    conn: psycopg.Connection, job_id: int, lease_token: str, status: str, cells: dict, error: str = None
) -> bool:
    """
    Saves the result of a job, with status 'done' or 'failed', and its sheet update.
    False means this worker no longer has the job, and nothing was saved
    """
    with conn.transaction():
        result = conn.execute(
            """
            UPDATE jobs SET status = %s, finished_at = now(), lease_until = NULL, error = %s
            WHERE id = %s AND lease_token = %s AND status = 'running'
            """,
            (status, error, job_id, lease_token),
        )
        if result.rowcount != 1:
            return False
        conn.execute(
            "INSERT INTO outbox (job_id, cells, expected_status) VALUES (%s, %s, %s)",
            (job_id, Jsonb(cells), STATUS_IN_PROGRESS),
        )
    return True


def release_setup_failure(conn: psycopg.Connection, job_id: int, lease_token: str) -> bool:
    """
    The child exited before work started, so the fault is in a config or VM rather than the row: requeue the job and
    give back its attempt. A sheet is paused at MAX_SETUP_FAILURES with one alert, and while it stays broken each
    further failure pauses it again with no new alert.
    False means this worker no longer has the job, and nothing was changed
    """
    with conn.transaction():
        job = conn.execute(
            """
            UPDATE jobs SET status = 'queued', attempts = attempts - 1,
                available_at = now() + interval '15 minutes',
                worker = NULL, lease_token = NULL, lease_until = NULL
            WHERE id = %s AND lease_token = %s AND status = 'running' AND work_started_at IS NULL
            RETURNING sheet_id
            """,
            (job_id, lease_token),
        ).fetchone()
        if job is None:
            return False
        sheet = conn.execute(
            """
            UPDATE sheets SET setup_failures = setup_failures + 1,
                paused_until = CASE WHEN setup_failures + 1 >= %s THEN now() + interval '15 minutes'
                                    ELSE paused_until END
            WHERE id = %s
            RETURNING id, setup_failures
            """,
            (MAX_SETUP_FAILURES, job["sheet_id"]),
        ).fetchone()
        if sheet["setup_failures"] == MAX_SETUP_FAILURES:
            add_alert(conn, "sheet_paused", f"Sheet {sheet['id']} paused after {MAX_SETUP_FAILURES} setup failures")
    return True


def requeue_expired(conn: psycopg.Connection) -> tuple[int, int]:
    """
    Cleanup: requeues running jobs whose lease expired over 2 minutes ago, and fails them after MAX_ATTEMPTS.
    The 2 minutes let supervisors renew after a Postgres restart. Returns (requeued, failed)
    """
    reason = f"lost its worker {MAX_ATTEMPTS} times - please rerun"
    cells = {"status": f"Archive failed - {reason}", "date": datetime.now(timezone.utc).isoformat()}
    with conn.transaction():
        failed = conn.execute(
            """
            WITH failed AS (
                UPDATE jobs SET status = 'failed', finished_at = now(), lease_until = NULL, error = %s
                WHERE status = 'running' AND lease_until < now() - interval '2 minutes' AND attempts >= %s
                RETURNING id
            )
            INSERT INTO outbox (job_id, cells, expected_status)
            SELECT id, %s, %s FROM failed
            """,
            (reason, MAX_ATTEMPTS, Jsonb(cells), STATUS_IN_PROGRESS),
        ).rowcount
        requeued = conn.execute(
            """
            UPDATE jobs SET status = 'queued', available_at = now() + interval '2 minutes',
                worker = NULL, lease_token = NULL, lease_until = NULL
            WHERE status = 'running' AND lease_until < now() - interval '2 minutes'
            """
        ).rowcount
    return requeued, failed
