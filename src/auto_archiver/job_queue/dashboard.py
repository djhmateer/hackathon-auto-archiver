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
from auto_archiver.job_queue.worker_supervisor import TIME_LIMIT_SECONDS, git_commit, machine_stats

REFRESH_SECONDS = 2
BACKUP_DIR = Path.home() / "backups"
BACKUP_LATE_SECONDS = 26 * 60 * 60  # nightly, plus some slack
RECENT_JOBS = 5
WAITING_JOBS = 10
TAB_WIDTH = 30
FAILURES = 5
SITES = 10
READ_LATE_SECONDS = 60  # sync reads every tab that's on every 15s
QUEUED_LONG_SECONDS = 60 * 60
WORKER_WIDTH = 12  # eg glanworker1
INDENT = " " * (WORKER_WIDTH + 3)  # lines under a worker's first line
GB, MB = 1024**3, 1024**2
DISK_LOW, DISK_VERY_LOW = 10 * GB, 5 * GB
# of memory still available
MEMORY_LOW, MEMORY_VERY_LOW = 0.25, 0.10
SWAP_HIGH = GB
CPU_HIGH = 90  # percent. A crawl uses a lot, so only yellow, and only near full

RED, YELLOW, GREEN, DIM, BOLD, RESET = "\033[31m", "\033[33m", "\033[32m", "\033[2m", "\033[1m", "\033[0m"
# redraw from the top left, clearing the rest of each line and of the screen, so the refresh doesn't flicker
HOME, CLEAR_LINE, CLEAR_BELOW = "\033[H", "\033[K", "\033[J"


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


def machine(stats: dict) -> str:
    """Disk, memory and swap, from machine_stats() or a worker's heartbeat"""
    if stats.get("disk_free_bytes") is None:
        return colour(DIM, "cpu, disk, memory and swap ?")  # sent from the next worker deploy
    free = stats["disk_free_bytes"]
    disk = colour(RED if free < DISK_VERY_LOW else YELLOW if free < DISK_LOW else GREEN, f"{free / GB:.1f} GB")
    share = stats["mem_available_bytes"] / stats["mem_total_bytes"]
    memory = colour(
        RED if share < MEMORY_VERY_LOW else YELLOW if share < MEMORY_LOW else GREEN,
        f"{stats['mem_available_bytes'] / GB:.1f} of {stats['mem_total_bytes'] / GB:.1f} GB",
    )
    swap_used = stats["swap_used_bytes"]
    swap = colour(YELLOW if swap_used > SWAP_HIGH else GREEN, f"{swap_used / MB:.0f} MB")
    cpu = "?" if stats.get("cpu_percent") is None else f"{stats['cpu_percent']:.0f}%"
    cpu = colour(YELLOW if stats.get("cpu_percent") and stats["cpu_percent"] > CPU_HIGH else GREEN, cpu)
    return f"cpu {cpu}, disk {disk} free, memory {memory} free, swap {swap} used"


def tab(row) -> str:
    """eg ClientA/Sheet1, shorter than db.describe_tab() to fit more on a line"""
    return f"{row['spreadsheet_name']}/{row['worksheet']}" if row["spreadsheet_name"] else row["worksheet"]


def heartbeat(seconds_ago: int) -> str:
    return colour(RED if seconds_ago >= WORKER_QUIET_SECONDS else GREEN, f"{ago(seconds_ago)} ago")


def header(conn, coordinator_stats: dict) -> list[str]:
    now = db.now(conn)
    lines = [
        colour(BOLD, f"Auto Archiver queue   {now:%Y-%m-%d %H:%M:%S} UTC")
        + colour(DIM, f"   refreshes every {REFRESH_SECONDS}s, Ctrl+C to quit")
    ]

    beat = conn.execute(
        "SELECT extract(epoch FROM now() - beat_at)::int AS seconds_ago FROM heartbeats WHERE name = 'coordinator'"
    ).fetchone()
    when = colour(RED, "no heartbeat ever") if beat is None else f"heartbeat {heartbeat(beat['seconds_ago'])}"
    lines.append(f"Coordinator: {when}, {machine(coordinator_stats)}")

    q = db.get_queue_summary(conn)
    lines.append(
        f"Queue: {q['queued']} queued, {q['running']} running, "
        f"{colour(YELLOW if q['unwritten'] else GREEN, q['unwritten'])} sheet writes to do, "
        f"{colour(RED if q['unsent_alerts'] else GREEN, q['unsent_alerts'])} unsent alerts"
    )

    speed = conn.execute(
        """
        SELECT count(*) FILTER (WHERE finished_at > now() - interval '1 hour') AS hour,
            count(*) FILTER (WHERE finished_at > now() - interval '1 day') AS day,
            (SELECT extract(epoch FROM now() - min(added_at))::int FROM jobs WHERE status = 'queued') AS oldest
        FROM jobs WHERE status IN ('done', 'failed')
        """
    ).fetchone()
    line = f"Speed: {speed['hour']} finished in the last hour, {speed['day']} in the last 24h"
    if speed["oldest"] is not None:
        oldest = colour(YELLOW if speed["oldest"] > QUEUED_LONG_SECONDS else GREEN, ago(speed["oldest"]))
        line += f", oldest queued job added {oldest} ago"
    lines.append(line)

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


def workers(conn, coordinator_commit: str) -> list[str]:
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
        counts = today.get(name, {"done": 0, "failed": 0})
        failed = colour(YELLOW if counts["failed"] else GREEN, counts["failed"])
        if beat["git_commit"] is None:
            code = colour(YELLOW, "?")  # sent from the next worker deploy
        else:
            # a worker that missed a deploy runs different code from the coordinator
            code = colour(GREEN if beat["git_commit"] == coordinator_commit else RED, beat["git_commit"])
        lines.append(
            f"  {name:<{WORKER_WIDTH}} heartbeat {heartbeat(beat['seconds_ago'])}, code {code}, "
            f"today {colour(GREEN, counts['done'])} done, {failed} failed"
        )
        lines.append(f"{INDENT}{machine(beat)}")
        job = running.get(name)
        if job is None:
            lines.append(colour(DIM, f"{INDENT}idle"))
        else:
            seconds = (datetime.now(timezone.utc) - job["started_at"]).total_seconds()
            took = colour(RED if seconds > TIME_LIMIT_SECONDS else GREEN, ago(seconds))
            lines.append(
                f"{INDENT}job {job['id']} {tab(job)} "
                f"row {job['row_number']} {job['domain']}, {took}, attempt {job['attempts']}"
            )
    return lines


def sheets(conn) -> list[str]:
    lines = [
        colour(BOLD, "Sheets")
        + colour(DIM, "   done and failed are for the last 24h; rows have a URL, blank of them have no status"),
        colour(
            DIM,
            f"  {'':<{TAB_WIDTH}} {'queued':>6} {'running':>7} {'done':>6} {'failed':>6} {'rows':>6} {'blank':>6}"
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
        name = tab(row)
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
            f"  {name:<{TAB_WIDTH}} {row['queued']:>6} {row['running']:>7} {done} {failed} {url_rows:>6} {blank_rows:>6}"
            f"   {read} {state}"
        )
    return lines


def sites(conn) -> list[str]:
    lines = [
        colour(BOLD, "Sites") + colour(DIM, f"   the {SITES} busiest in the last 24h"),
        colour(DIM, f"  {'':<30} {'done':>6} {'failed':>6} {'blocked':>7}   {'average':<8}"),
    ]
    rows = conn.execute(
        """
        SELECT j.domain,
            count(*) FILTER (WHERE j.status = 'done' AND NOT w.blocked) AS done,
            count(*) FILTER (WHERE j.status = 'failed' AND NOT w.blocked) AS failed,
            count(*) FILTER (WHERE w.blocked) AS blocked,
            extract(epoch FROM avg(j.finished_at - j.started_at))::int AS average
        FROM jobs j
        LEFT JOIN LATERAL (
            SELECT coalesce(cells->>'status' ILIKE '%%blocked%%', false) AS blocked
            FROM sheet_writes WHERE job_id = j.id ORDER BY id DESC LIMIT 1
        ) w ON true
        WHERE j.status IN ('done', 'failed') AND j.finished_at > now() - interval '1 day'
        GROUP BY j.domain ORDER BY count(*) DESC, j.domain LIMIT %s
        """,
        (SITES,),
    ).fetchall()
    if not rows:
        lines.append(colour(DIM, "  no jobs finished"))
    for row in rows:
        failed = colour(YELLOW if row["failed"] else GREEN, str(row["failed"]).rjust(6))
        blocked = colour(RED if row["blocked"] else GREEN, str(row["blocked"]).rjust(7))
        average = ago(row["average"]) if row["average"] is not None else "?"
        lines.append(
            f"  {row['domain'][:30]:<30} {colour(GREEN, str(row['done']).rjust(6))} {failed} {blocked}   {average}"
        )
    return lines


def storage(conn) -> list[str]:
    files = conn.execute(
        "SELECT count(*) AS files, coalesce(sum(bytes), 0) AS bytes FROM job_files "
        "WHERE created_at > now() - interval '1 day'"
    ).fetchone()
    broken = conn.execute(
        "SELECT id, store_failures FROM jobs WHERE store_failures > 0 AND finished_at > now() - interval '1 day' "
        "ORDER BY id DESC"
    ).fetchall()
    line = f"Storage (24h): {files['files']} files, {files['bytes'] / GB:.2f} GB stored"
    if broken:
        jobs = ", ".join(f"job {row['id']} ({row['store_failures']})" for row in broken[:FAILURES])
        line += ", " + colour(RED, f"{len(broken)} job(s) with files that couldn't be stored: {jobs}")
    else:
        line += ", " + colour(GREEN, "every file stored")
    return [line]


FINISHED_JOBS = """
    SELECT j.id, j.status, j.worker, j.row_number, j.domain, j.started_at, j.finished_at, j.error,
        s.spreadsheet_name, s.worksheet, w.cells->>'status' AS sheet_status
    FROM jobs j JOIN sheets s ON s.id = j.sheet_id
    LEFT JOIN LATERAL (
        SELECT cells FROM sheet_writes WHERE job_id = j.id ORDER BY id DESC LIMIT 1
    ) w ON true
    WHERE j.status IN ('done', 'failed') {where}
    ORDER BY j.finished_at DESC LIMIT %s
"""


def is_ok(row) -> bool:
    return row["status"] == "done" and "blocked" not in (row["sheet_status"] or "")


def job_line(row) -> str:
    took = ago((row["finished_at"] - row["started_at"]).total_seconds()) if row["started_at"] else "?"
    result = colour(GREEN if is_ok(row) else YELLOW, (row["sheet_status"] or row["status"])[:80])
    return (
        f"  {row['finished_at']:%H:%M} {row['worker'] or '':<{WORKER_WIDTH}} job {row['id']} "
        f"{tab(row)} row {row['row_number']} "
        f"{row['domain']}, {took}: {result}"
    )


def recent(conn) -> list[str]:
    rows = conn.execute(FINISHED_JOBS.format(where=""), (RECENT_JOBS,)).fetchall()
    return [colour(BOLD, f"Last {RECENT_JOBS} finished")] + [job_line(row) for row in rows]


def failures(conn) -> list[str]:
    where = "AND (j.status = 'failed' OR w.cells->>'status' ILIKE '%%blocked%%')"
    rows = conn.execute(FINISHED_JOBS.format(where=where), (FAILURES,)).fetchall()
    lines = [colour(BOLD, f"Last {FAILURES} failed or blocked")]
    if not rows:
        lines.append(colour(GREEN, "  none"))
    return lines + [job_line(row) for row in rows]


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
    for row in held[:WAITING_JOBS]:
        reason = row["reason"]
        if reason == "retry":
            reason = f"retry at {row['available_at']:%H:%M} UTC, attempt {row['attempts'] + 1}"
        lines.append(f"  job {row['id']} {tab(row)} row {row['row_number']} {row['domain']}: {colour(YELLOW, reason)}")
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


def render(conn, coordinator_commit: str = "?", coordinator_stats: dict = None) -> str:
    sections = [
        header(conn, coordinator_stats or {}),
        workers(conn, coordinator_commit),
        sheets(conn),
        waiting(conn),
        sites(conn),
        storage(conn),
        failures(conn),
        recent(conn),
        alerts(conn),
    ]
    return "\n\n".join("\n".join(section) for section in sections)


def main() -> None:
    while True:
        # a new connection each time, so a Postgres restart only loses one refresh
        try:
            with db.connect() as conn:
                # the dashboard runs on the coordinator, from its repo, so these are the coordinator's, to compare
                # the workers' code with. Each time, so it shows a deploy without restarting the dashboard
                screen = render(conn, git_commit(), machine_stats("."))
        except Exception as e:
            screen = colour(RED, f"Can't read the queue database: {e}")
        print(HOME + screen.replace("\n", CLEAR_LINE + "\n") + CLEAR_LINE + CLEAR_BELOW, end="", flush=True)
        try:
            time.sleep(REFRESH_SECONDS)
        except KeyboardInterrupt:
            print()
            return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
