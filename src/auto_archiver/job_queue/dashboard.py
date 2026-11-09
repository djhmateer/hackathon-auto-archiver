"""
A console dashboard for the queue, run on the coordinator. Read only: it never writes to the database.

    poetry run python -m auto_archiver.job_queue.dashboard

Redraws every few seconds until Ctrl+C. Green is fine, yellow is worth a look, red needs action.
"""

import time
from datetime import datetime, timezone
from pathlib import Path

from auto_archiver.job_queue import db
from auto_archiver.job_queue.coordinator import WORKER_QUIET_SECONDS
from auto_archiver.job_queue.worker_supervisor import TIME_LIMIT_SECONDS

REFRESH_SECONDS = 5
BACKUP_DIR = Path.home() / "backups"
BACKUP_LATE_SECONDS = 26 * 60 * 60  # nightly, plus some slack
RECENT_JOBS = 10
READ_LATE_SECONDS = 60  # sync reads every tab that's on every 15s

RED, YELLOW, GREEN, DIM, BOLD, RESET = "\033[31m", "\033[33m", "\033[32m", "\033[2m", "\033[1m", "\033[0m"
CLEAR_SCREEN = "\033[H\033[2J"


def colour(code: str, text) -> str:
    return f"{code}{text}{RESET}"


def ago(seconds) -> str:
    """eg 45s, 12m, 3h 5m, 2d 4h"""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 60 * 60:
        return f"{seconds // 60}m"
    if seconds < 24 * 60 * 60:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"
    return f"{seconds // 86400}d {seconds % 86400 // 3600}h"


def header(conn) -> list[str]:
    now = db.now(conn)
    lines = [
        colour(BOLD, f"Auto Archiver queue   {now:%Y-%m-%d %H:%M:%S} UTC")
        + colour(DIM, f"   refreshes every {REFRESH_SECONDS}s, Ctrl+C to quit")
    ]

    beat = conn.execute(
        "SELECT extract(epoch FROM now() - beat_at)::int AS seconds_ago FROM heartbeats WHERE name = 'coordinator'"
    ).fetchone()
    if beat is None:
        lines.append(f"Coordinator: {colour(RED, 'no heartbeat ever')}")
    else:
        late = beat["seconds_ago"] >= WORKER_QUIET_SECONDS
        when = colour(RED if late else GREEN, ago(beat["seconds_ago"]) + " ago")
        lines.append(f"Coordinator: heartbeat {when}")

    q = db.get_queue_summary(conn)
    lines.append(
        f"Queue: {q['queued']} queued, {q['running']} running, "
        f"{colour(YELLOW if q['unwritten'] else GREEN, q['unwritten'])} sheet writes to do, "
        f"{colour(RED if q['unsent_alerts'] else GREEN, q['unsent_alerts'])} unsent alerts"
    )

    backups = sorted(BACKUP_DIR.glob("auto_archiver_*.dump"), key=lambda p: p.stat().st_mtime)
    if not backups:
        lines.append(f"Last backup: {colour(RED, f'none in {BACKUP_DIR}')}")
    else:
        newest = backups[-1]
        age = time.time() - newest.stat().st_mtime
        size = f"{newest.stat().st_size / 1024 / 1024:.1f} MB"
        when = colour(RED if age > BACKUP_LATE_SECONDS else GREEN, ago(age) + " ago")
        lines.append(f"Last backup: {when}, {newest.name}, {size}")
    return lines


def workers(conn) -> list[str]:
    lines = [colour(BOLD, "Workers")]
    running = {job["worker"]: job for job in db.get_running_jobs(conn)}
    today = {
        row["worker"]: row
        for row in conn.execute(
            """
            SELECT worker, count(*) FILTER (WHERE status = 'done') AS done,
                count(*) FILTER (WHERE status = 'failed') AS failed
            FROM jobs WHERE finished_at >= date_trunc('day', now()) GROUP BY worker
            """
        )
    }
    beats = db.get_worker_heartbeats(conn)
    if not beats:
        lines.append(colour(RED, "  No worker has ever sent a heartbeat"))
    for beat in beats:
        name = beat["worker"]
        late = beat["seconds_ago"] >= WORKER_QUIET_SECONDS
        counts = today.get(name, {"done": 0, "failed": 0})
        when = colour(RED if late else GREEN, (ago(beat["seconds_ago"]) + " ago").ljust(8))
        failed = colour(YELLOW if counts["failed"] else GREEN, counts["failed"])
        line = f"  {name:<10} heartbeat {when} today {colour(GREEN, counts['done'])} done, {failed} failed   "
        job = running.get(name)
        if job is None:
            line += colour(DIM, "idle")
        else:
            seconds = (datetime.now(timezone.utc) - job["started_at"]).total_seconds()
            took = colour(RED if seconds > TIME_LIMIT_SECONDS else GREEN, ago(seconds))
            line += (
                f"job {job['id']} {db.describe_tab(job['spreadsheet_name'], job['worksheet'])} row {job['row_number']} "
                f"{job['domain']}, {took}, attempt {job['attempts']}"
            )
        lines.append(line)
    return lines


def sheets(conn) -> list[str]:
    lines = [
        colour(BOLD, "Sheets")
        + colour(DIM, "   done and failed are for the last 24h; rows have a URL, blank of them have no status"),
        colour(
            DIM,
            f"  {'':<40} {'queued':>6} {'running':>7} {'done':>6} {'failed':>6} {'rows':>6} {'blank':>6}"
            f"   {'last read':<9}",
        ),
    ]
    rows = conn.execute(
        """
        SELECT s.spreadsheet_name, s.worksheet, s.enabled, s.paused_until > now() AS paused, s.paused_until,
            s.url_rows, s.blank_rows, extract(epoch FROM now() - s.last_read_at)::int AS read_seconds_ago,
            count(j.id) FILTER (WHERE j.status = 'queued') AS queued,
            count(j.id) FILTER (WHERE j.status = 'running') AS running,
            count(j.id) FILTER (WHERE j.status = 'done' AND j.finished_at > now() - interval '1 day') AS done,
            count(j.id) FILTER (WHERE j.status = 'failed' AND j.finished_at > now() - interval '1 day') AS failed
        FROM sheets s LEFT JOIN jobs j ON j.sheet_id = s.id
        GROUP BY s.id ORDER BY s.enabled DESC, s.id
        """
    ).fetchall()
    for row in rows:
        if row["paused"]:
            state = colour(RED, f"paused until {row['paused_until']:%H:%M} UTC")
        elif row["enabled"]:
            state = colour(GREEN, "on")
        else:
            state = colour(DIM, "off")
        name = db.describe_tab(row["spreadsheet_name"], row["worksheet"])
        done = colour(GREEN, str(row["done"]).rjust(6))
        failed = colour(YELLOW if row["failed"] else GREEN, str(row["failed"]).rjust(6))
        url_rows = "" if row["url_rows"] is None else row["url_rows"]
        blank_rows = "" if row["blank_rows"] is None else row["blank_rows"]
        seconds = row["read_seconds_ago"]
        read = "never" if seconds is None else f"{ago(seconds)} ago"
        # a tab that's off isn't read, so only an enabled one can be late
        if not row["enabled"]:
            read_colour = DIM
        elif seconds is None or seconds > READ_LATE_SECONDS:
            read_colour = RED
        else:
            read_colour = GREEN
        read = colour(read_colour, read.ljust(9))
        lines.append(
            f"  {name:<40} {row['queued']:>6} {row['running']:>7} {done} {failed} {url_rows:>6} {blank_rows:>6}"
            f"   {read} {state}"
        )
    return lines


def waiting(conn) -> list[str]:
    """Queued jobs that can't be claimed right now, and why"""
    rows = conn.execute(
        """
        SELECT j.id, j.row_number, j.domain, j.attempts, j.available_at, s.spreadsheet_name, s.worksheet,
            CASE
                WHEN NOT s.enabled THEN 'sheet is off'
                WHEN s.paused_until > now() THEN 'sheet is paused'
                WHEN j.available_at > now() THEN 'retry'
                WHEN EXISTS (SELECT 1 FROM jobs b WHERE b.domain = j.domain AND b.status = 'running')
                    THEN 'site busy'
            END AS reason
        FROM jobs j JOIN sheets s ON s.id = j.sheet_id
        WHERE j.status = 'queued'
        ORDER BY j.id
        """
    ).fetchall()
    held = [row for row in rows if row["reason"]]
    lines = [colour(BOLD, "Waiting") + colour(DIM, f"   {len(held)} queued job(s) that can't be claimed yet")]
    for row in held[:RECENT_JOBS]:
        reason = row["reason"]
        if reason == "retry":
            reason = f"retry at {row['available_at']:%H:%M} UTC, attempt {row['attempts'] + 1}"
        lines.append(
            f"  job {row['id']} {db.describe_tab(row['spreadsheet_name'], row['worksheet'])} row {row['row_number']} "
            f"{row['domain']}: {colour(YELLOW, reason)}"
        )
    return lines


def recent(conn) -> list[str]:
    lines = [colour(BOLD, f"Last {RECENT_JOBS} finished")]
    rows = conn.execute(
        """
        SELECT j.id, j.status, j.worker, j.row_number, j.domain, j.started_at, j.finished_at,
            s.spreadsheet_name, s.worksheet, w.cells->>'status' AS sheet_status
        FROM jobs j JOIN sheets s ON s.id = j.sheet_id
        LEFT JOIN LATERAL (
            SELECT cells FROM sheet_writes WHERE job_id = j.id ORDER BY id DESC LIMIT 1
        ) w ON true
        WHERE j.status IN ('done', 'failed')
        ORDER BY j.finished_at DESC LIMIT %s
        """,
        (RECENT_JOBS,),
    ).fetchall()
    for row in rows:
        ok = row["status"] == "done" and "blocked" not in (row["sheet_status"] or "")
        took = ago((row["finished_at"] - row["started_at"]).total_seconds()) if row["started_at"] else "?"
        result = colour(GREEN if ok else YELLOW, (row["sheet_status"] or row["status"])[:80])
        lines.append(
            f"  {row['finished_at']:%H:%M} {row['worker'] or '':<9} job {row['id']} "
            f"{db.describe_tab(row['spreadsheet_name'], row['worksheet'])} row {row['row_number']} "
            f"{row['domain']}, {took}: {result}"
        )
    return lines


def alerts(conn) -> list[str]:
    lines = [colour(BOLD, "Alerts") + colour(DIM, "   unsent, then the last 5 sent in the last 24h")]
    rows = conn.execute(
        """
        (SELECT * FROM admin_alerts WHERE sent_at IS NULL ORDER BY id)
        UNION ALL
        (SELECT * FROM admin_alerts WHERE sent_at IS NOT NULL AND created_at > now() - interval '1 day'
            ORDER BY id DESC LIMIT 5)
        """
    ).fetchall()
    if not rows:
        lines.append(colour(GREEN, "  none"))
    for row in rows:
        # the first line only: later lines can hold the row's URL
        message = row["message"].splitlines()[0][:100]
        line = f"  {row['created_at']:%m-%d %H:%M} {row['kind']}: {message}"
        lines.append(colour(RED, line + "  (unsent)") if row["sent_at"] is None else colour(DIM, line))
    return lines


def render(conn) -> str:
    sections = [header(conn), workers(conn), sheets(conn), waiting(conn), recent(conn), alerts(conn)]
    return "\n\n".join("\n".join(section) for section in sections)


def main() -> None:
    while True:
        # a new connection each time, so a Postgres restart only loses one refresh
        try:
            with db.connect() as conn:
                screen = render(conn)
        except Exception as e:
            screen = colour(RED, f"Can't read the queue database: {e}")
        print(CLEAR_SCREEN + screen, flush=True)
        try:
            time.sleep(REFRESH_SECONDS)
        except KeyboardInterrupt:
            return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
