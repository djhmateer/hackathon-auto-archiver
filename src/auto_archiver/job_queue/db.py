"""
All the SQL for the job queue. See queue-spec.md

Only imports psycopg and the standard library, so the coordinator, supervisor and queue_feeder_db can all use it.
Every function takes an open connection from connect(), and anything that writes twice uses a transaction.
"""

from pathlib import Path
from datetime import datetime, timezone

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

STATUS_QUEUED = "Queued"
STATUS_IN_PROGRESS = "Archive in progress"
# any fixed number, for the advisory lock that makes claims take turns
CLAIM_LOCK = 4_242_001


def in_progress_status(worker: str) -> str:
    """What the sheet shows while a job runs, eg 'Archive in progress (vm1)'"""
    return f"{STATUS_IN_PROGRESS} ({worker})"


def is_in_progress(status: str) -> bool:
    """With or without the worker's name, as rows written before it was added have none"""
    return status == STATUS_IN_PROGRESS or status.startswith(f"{STATUS_IN_PROGRESS} (")


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


def get_last_cells(conn: psycopg.Connection, job_id: int) -> dict:
    """The cells of the job's last sheet update, eg its result"""
    row = conn.execute(
        "SELECT cells FROM sheet_writes WHERE job_id = %s ORDER BY id DESC LIMIT 1", (job_id,)
    ).fetchone()
    return row["cells"] if row else {}


def describe_tab(spreadsheet_name: str, tab_title: str) -> str:
    """eg Sheet 'AA Dave Testing' tab Sheet1. The name is empty until sync has read the spreadsheet"""
    return f"Sheet '{spreadsheet_name}' tab {tab_title}" if spreadsheet_name else f"tab {tab_title}"


def describe_config(config_path: str) -> str:
    """A config's name for logging, without its folder and .yaml, eg orchestration-aa-dave-testing"""
    return Path(config_path).stem


def describe_database(url: str = None) -> str:
    """user@host:port/dbname, without the password, for logging"""
    info = conninfo_to_dict(url or database_url())
    return f"{info.get('user', '')}@{info.get('host', 'localhost')}:{info.get('port', 5432)}/{info.get('dbname', '')}"


def connect(url: str = None) -> psycopg.Connection:
    """autocommit, so a single statement commits by itself and several use `with conn.transaction()`"""
    return psycopg.connect(url or database_url(), autocommit=True, row_factory=dict_row)


def add_alert(conn: psycopg.Connection, kind: str, message: str) -> None:
    conn.execute("INSERT INTO admin_alerts (kind, message) VALUES (%s, %s)", (kind, message))


def add_alert_once(conn: psycopg.Connection, kind: str, message: str) -> None:
    """For something checked every cycle: at most one alert a day for the same thing"""
    conn.execute(
        """
        INSERT INTO admin_alerts (kind, message)
        SELECT %(kind)s, %(message)s
        WHERE NOT EXISTS (
            SELECT 1 FROM admin_alerts WHERE kind = %(kind)s AND message = %(message)s
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
    conn: psycopg.Connection,
    spreadsheet_id: str,
    gid: int,
    title: str,
    config: str,
    update_config: bool = False,
    spreadsheet_name: str = "",
) -> None:
    """
    Adds a tab, off so cron keeps it until it's moved. A tab added to a spreadsheet already on the queue starts on,
    as cron no longer reads that spreadsheet. A known tab only gets its current title, and its config when registering:
    sync can't change it, or a tab allowed by two configs would swap between them every cycle
    """
    conn.execute(
        """
        INSERT INTO sheets (spreadsheet_id, worksheet_gid, worksheet, config, enabled, spreadsheet_name)
        VALUES (%(id)s, %(gid)s, %(title)s, %(config)s,
                EXISTS (SELECT 1 FROM sheets WHERE spreadsheet_id = %(id)s AND enabled), %(name)s)
        ON CONFLICT (spreadsheet_id, worksheet_gid) DO UPDATE SET worksheet = EXCLUDED.worksheet,
            config = CASE WHEN %(update_config)s THEN EXCLUDED.config ELSE sheets.config END,
            spreadsheet_name = CASE WHEN %(name)s = '' THEN sheets.spreadsheet_name ELSE %(name)s END
        """,
        {
            "id": spreadsheet_id,
            "gid": gid,
            "title": title,
            "config": config,
            "update_config": update_config,
            "name": spreadsheet_name,
        },
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
        SELECT count(*) AS n FROM sheet_writes w JOIN jobs j ON j.id = w.job_id JOIN sheets s ON s.id = j.sheet_id
        WHERE s.spreadsheet_id = %s AND w.written_at IS NULL AND w.discarded_at IS NULL
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
            (SELECT count(*) FROM sheet_writes WHERE written_at IS NULL AND discarded_at IS NULL) AS unwritten,
            (SELECT count(*) FROM admin_alerts WHERE sent_at IS NULL) AS unsent_alerts
        """
    ).fetchone()


def get_worker_heartbeats(conn: psycopg.Connection) -> list[dict]:
    """Each worker supervisor's name and how many seconds ago it last beat"""
    return conn.execute(
        """
        SELECT substr(name, length('worker-supervisor-') + 1) AS worker,
            extract(epoch FROM now() - beat_at)::int AS seconds_ago
        FROM heartbeats WHERE name LIKE 'worker-supervisor-%' ORDER BY name
        """
    ).fetchall()


def get_jobs_after(conn: psycopg.Connection, job_id: int) -> list[dict]:
    return conn.execute(
        """
        SELECT j.*, s.worksheet, s.config, s.spreadsheet_name FROM jobs j JOIN sheets s ON s.id = j.sheet_id
        WHERE j.id > %s ORDER BY j.id
        """,
        (job_id,),
    ).fetchall()


def get_running_jobs(conn: psycopg.Connection) -> list[dict]:
    return conn.execute(
        """
        SELECT j.*, s.worksheet, s.config, s.spreadsheet_name FROM jobs j JOIN sheets s ON s.id = j.sheet_id
        WHERE j.status = 'running' ORDER BY j.id
        """
    ).fetchall()


def get_jobs_waiting_for_site(conn: psycopg.Connection) -> list[dict]:
    """Queued jobs that could be claimed now, except that another job for their site is running"""
    return conn.execute(
        """
        SELECT j.*, s.worksheet, s.config, s.spreadsheet_name, busy.id AS busy_job_id, busy.worker AS busy_worker
        FROM jobs j JOIN sheets s ON s.id = j.sheet_id
        JOIN jobs busy ON busy.domain = j.domain AND busy.status = 'running'
        WHERE j.status = 'queued' AND j.available_at <= now() AND s.enabled
            AND (s.paused_until IS NULL OR s.paused_until <= now())
        ORDER BY j.id
        """
    ).fetchall()


def add_job(
    conn: psycopg.Connection,
    sheet_id: int,
    row_number: int,
    url: str,
    domain: str,
    folder: str = "",
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
            "INSERT INTO sheet_writes (job_id, cells, expected_status) VALUES (%s, %s, '')",
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
            "INSERT INTO sheet_writes (job_id, cells, expected_status) VALUES (%s, %s, '')",
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
            "SELECT 1 FROM sheet_writes WHERE job_id = %s AND expected_status = %s", (job_id, STATUS_QUEUED)
        ).fetchone()
        # skips the URL check, which fails by definition when the URL has changed
        conn.execute(
            "INSERT INTO sheet_writes (job_id, cells, expected_status, skip_url_check) VALUES (%s, %s, %s, true)",
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
            COALESCE(bool_or(w.id IS NOT NULL AND w.written_at IS NULL AND w.discarded_at IS NULL), false)
                AS has_unwritten,
            COALESCE(bool_or(w.written_at >= %(snapshot_at)s), false) AS written_since_snapshot
        FROM jobs j LEFT JOIN sheet_writes w ON w.job_id = j.id
        WHERE j.sheet_id = %(sheet_id)s AND (
            j.status IN ('queued', 'running')
            OR (w.id IS NOT NULL AND w.written_at IS NULL AND w.discarded_at IS NULL)
            OR w.written_at >= %(snapshot_at)s
        )
        GROUP BY j.row_number
        """,
        {"sheet_id": sheet_id, "snapshot_at": snapshot_at},
    ).fetchall()


def get_next_sheet_writes(conn: psycopg.Connection) -> list[dict]:
    """The latest unwritten entry of each job, as it has the row's current status. Earlier ones aren't needed"""
    return conn.execute(
        """
        SELECT * FROM (
            SELECT DISTINCT ON (w.job_id) w.id, w.job_id, w.cells, w.skip_url_check,
                j.row_number, j.url, s.id AS sheet_id, s.spreadsheet_id, s.worksheet_gid, s.config
            FROM sheet_writes w JOIN jobs j ON j.id = w.job_id JOIN sheets s ON s.id = j.sheet_id
            WHERE w.written_at IS NULL AND w.discarded_at IS NULL
            ORDER BY w.job_id, w.id DESC
        ) latest_entries
        ORDER BY id
        """
    ).fetchall()


def mark_written(conn: psycopg.Connection, write_ids: list[int]) -> None:
    """
    Marks each entry written, with its job's earlier unwritten entries, which it replaced on the sheet.
    Only once the sheet has been updated, as sync rule 1 relies on that
    """
    conn.execute(
        """
        UPDATE sheet_writes w SET written_at = now()
        FROM sheet_writes latest
        WHERE latest.id = ANY(%s) AND w.job_id = latest.job_id AND w.id <= latest.id
            AND w.written_at IS NULL AND w.discarded_at IS NULL
        """,
        (write_ids,),
    )


def discard_job_writes(conn: psycopg.Connection, job_id: int, message: str) -> None:
    """Drops a job's unwritten entries, when they can't go to the sheet, and raises an alert"""
    with conn.transaction():
        conn.execute(
            "UPDATE sheet_writes SET discarded_at = now() WHERE job_id = %s AND written_at IS NULL "
            "AND discarded_at IS NULL",
            (job_id,),
        )
        add_alert(conn, "writer_discarded", message)


def count_active_jobs(conn: psycopg.Connection) -> dict:
    """How many jobs are queued and running, on every worker"""
    rows = conn.execute(
        "SELECT status, count(*) AS n FROM jobs WHERE status IN ('queued', 'running') GROUP BY status"
    ).fetchall()
    return {"queued": 0, "running": 0, **{row["status"]: row["n"] for row in rows}}


def claim_job(conn: psycopg.Connection, worker: str, git_commit: str = None) -> dict | None:
    """
    Claims the next job, or returns None if there isn't one. The sheet with the fewest running jobs for its weight
    goes first, then the oldest job.

    Skips a job whose domain (its site, eg vimeo.com) already has a job running, so no site gets more than one at a
    time. The lock makes claims take turns, or two workers could each see the site free and both claim it.

    Only the job's first claim adds the in progress status. That's found from its sheet writes rather than attempts, because
    a setup failure gives its attempt back.
    """
    with conn.transaction():
        # released at the end of the transaction. The UPDATE after it sees any claim committed while it waited
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (CLAIM_LOCK,))
        job = conn.execute(
            """
            UPDATE jobs SET status = 'running', worker = %s, git_commit = %s, attempts = attempts + 1,
                started_at = COALESCE(started_at, now()), work_started_at = NULL,
                lease_token = gen_random_uuid(), lease_until = now() + interval '5 minutes'
            FROM sheets sheet
            WHERE sheet.id = jobs.sheet_id AND jobs.id = (
                SELECT j.id FROM jobs j JOIN sheets s ON s.id = j.sheet_id
                WHERE j.status = 'queued' AND j.available_at <= now() AND s.enabled
                    AND (s.paused_until IS NULL OR s.paused_until <= now())
                    AND NOT EXISTS (SELECT 1 FROM jobs busy WHERE busy.domain = j.domain AND busy.status = 'running')
                ORDER BY (SELECT count(*) FROM jobs r WHERE r.sheet_id = j.sheet_id
                          AND r.status = 'running')::float / s.weight, j.id
                FOR UPDATE OF j SKIP LOCKED LIMIT 1
            )
            RETURNING jobs.*, sheet.config, sheet.worksheet, sheet.spreadsheet_name
            """,
            (worker, git_commit),
        ).fetchone()
        if job is None:
            return None
        conn.execute(
            """
            INSERT INTO sheet_writes (job_id, cells, expected_status)
            SELECT %(id)s, %(cells)s, %(queued)s
            WHERE NOT EXISTS (SELECT 1 FROM sheet_writes WHERE job_id = %(id)s AND expected_status = %(queued)s)
            """,
            {"id": job["id"], "cells": Jsonb({"status": in_progress_status(worker)}), "queued": STATUS_QUEUED},
        )
    return job


def get_job(conn: psycopg.Connection, job_id: int) -> dict | None:
    return conn.execute(
        """
        SELECT j.*, s.spreadsheet_id, s.worksheet, s.config, s.spreadsheet_name
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


RESULT_COLUMNS = ("title", "archive_url", "extractor", "posted_at", "cached", "store_failures")
FILE_COLUMNS = ("role", "key", "url", "mimetype", "bytes", "hash", "duration_seconds")


def finish_job(
    conn: psycopg.Connection,
    job_id: int,
    lease_token: str,
    status: str,
    cells: dict,
    error: str = None,
    result: dict = None,
    files: list[dict] = (),
) -> bool:
    """
    Saves the result of a job, with status 'done' or 'failed', and its sheet update. result has the RESULT_COLUMNS
    and each of files the FILE_COLUMNS, for the dashboard. False means this worker no longer has the job, and nothing
    was saved
    """
    result = result or {}
    with conn.transaction():
        job = conn.execute(
            f"""
            UPDATE jobs SET status = %s, finished_at = now(), lease_until = NULL, error = %s,
                {", ".join(f"{column} = %s" for column in RESULT_COLUMNS)}
            WHERE id = %s AND lease_token = %s AND status = 'running'
            RETURNING attempts
            """,
            (status, error, *(result.get(column) for column in RESULT_COLUMNS), job_id, lease_token),
        ).fetchone()
        if job is None:
            return False
        conn.execute(
            "INSERT INTO sheet_writes (job_id, cells, expected_status) VALUES (%s, %s, %s)",
            (job_id, Jsonb(cells), STATUS_IN_PROGRESS),
        )
        for file in files:
            conn.execute(
                f"""
                INSERT INTO job_files (job_id, attempt, {", ".join(FILE_COLUMNS)})
                VALUES (%s, %s, {", ".join(["%s"] * len(FILE_COLUMNS))})
                """,
                (job_id, job["attempts"], *(file.get(column) for column in FILE_COLUMNS)),
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
            INSERT INTO sheet_writes (job_id, cells, expected_status)
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
