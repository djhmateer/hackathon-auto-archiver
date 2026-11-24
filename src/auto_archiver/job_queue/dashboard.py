"""
A console dashboard for the queue, run on the coordinator. Read only: it never writes to the database.

    poetry run python -m auto_archiver.job_queue.dashboard

Redraws every few seconds until Ctrl+C. Green is fine, yellow is worth a look, red needs action. p shows one pool
at a time, then all again: every section only has that pool's sheets, jobs and workers, so a screenshot can go to a
client without the other clients' data. The shared pool's workers are shown too, as they can work for any pool, but
one running another pool's job only says it's busy.
"""

import select
import sys
import termios
import time
import tty
from datetime import datetime, timezone
from pathlib import Path

from auto_archiver.job_queue import db
from auto_archiver.job_queue.coordinator import SYNC_SECONDS, WORKER_QUIET_SECONDS
from auto_archiver.job_queue.stage import time_limit_seconds
from auto_archiver.job_queue.sync import ACTIVE_DAYS, QUIET_TAB_SECONDS
from auto_archiver.job_queue.worker_supervisor import git_commit, machine_stats

REFRESH_SECONDS = 2
BACKUP_DIR = Path.home() / "backups"
BACKUP_LATE_SECONDS = 26 * 60 * 60  # nightly, plus some slack
RECENT_JOBS = 5
WAITING_JOBS = 10
TAB_WIDTH = 40
SHEET_MIN = 10  # the fewest characters of a spreadsheet's name a cut tab name keeps
FAILURES = 5
SITES = 10
# a quiet tab is due every QUIET_TAB_SECONDS, but read only when a pass starts, and after the spreadsheets before it
# in that pass (~50s when all the quiet tabs are due together), so allow for both
READ_LATE_SECONDS = QUIET_TAB_SECONDS + 3 * 60
READ_GAP_MINUTES = 20  # the slowest gap between a tab's reads over this long (sheet_reads keeps an hour)
QUEUED_LONG_SECONDS = 60 * 60
WORKER_WIDTH = 12  # eg glanworker1
POOL_WIDTH = 5  # eg glan
INDENT = " " * (WORKER_WIDTH + 3)  # lines under a worker's first line
GB, MB = 1024**3, 1024**2
DISK_LOW, DISK_VERY_LOW = 10 * GB, 5 * GB
# of memory still available
MEMORY_LOW, MEMORY_VERY_LOW = 0.25, 0.10
BAR_WIDTH = 10  # blocks in the memory bar, each one a tenth of the total
SWAP_HIGH = GB
CPU_HIGH = 90  # percent. A crawl uses a lot, so only yellow, and only near full

RED, YELLOW, GREEN, DIM, BOLD, RESET = "\033[31m", "\033[33m", "\033[32m", "\033[2m", "\033[1m", "\033[0m"
# redraw from the top left, clearing the rest of each line and of the screen, so the refresh doesn't flicker
HOME, CLEAR_LINE, CLEAR_BELOW = "\033[H", "\033[K", "\033[J"
# the sheets of one pool, or all of them when pool is None. For a query with the sheet as s and a pool parameter
IN_POOL = "(%(pool)s::text IS NULL OR s.organisation_id IN (SELECT id FROM organisations WHERE pool = %(pool)s))"


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
    available, total = stats["mem_available_bytes"], stats["mem_total_bytes"]
    share = available / total
    used_blocks = round(BAR_WIDTH * (1 - share))
    bar = "█" * used_blocks + "░" * (BAR_WIDTH - used_blocks)
    memory = colour(
        RED if share < MEMORY_VERY_LOW else YELLOW if share < MEMORY_LOW else GREEN,
        f"[{bar}] {(total - available) / GB:.1f} GB used, {available / GB:.1f} GB",
    )
    swap_used = stats["swap_used_bytes"]
    swap = colour(YELLOW if swap_used > SWAP_HIGH else GREEN, f"{swap_used / MB:.0f} MB")
    cpu = "?" if stats.get("cpu_percent") is None else f"{stats['cpu_percent']:.0f}%"
    cpu = colour(YELLOW if stats.get("cpu_percent") and stats["cpu_percent"] > CPU_HIGH else GREEN, cpu)
    return f"cpu {cpu}, disk {disk} free, memory {memory} available, swap {swap} used"


def tab(row) -> str:
    """eg ClientA/Sheet1, shorter than db.describe_tab() to fit more on a line"""
    return f"{row['spreadsheet_name']}/{row['worksheet']}" if row["spreadsheet_name"] else row["worksheet"]


def tab_column(row) -> str:
    """
    tab(), cut to TAB_WIDTH for a column. First the end of the spreadsheet's name, as the tab's name is what tells a
    spreadsheet's tabs apart, then the middle, which keeps both ends. eg Indigo Datasheet - Israel…/Archiving_SAT
    """
    name = tab(row)
    sheet, worksheet = row["spreadsheet_name"], row["worksheet"]
    if len(name) > TAB_WIDTH and sheet:
        room = max(SHEET_MIN, TAB_WIDTH - len(worksheet) - 1)
        if room < len(sheet):
            name = f"{sheet[: room - 1]}…/{worksheet}"
    if len(name) > TAB_WIDTH:
        head = (TAB_WIDTH - 1) // 2
        name = name[:head] + "…" + name[-(TAB_WIDTH - 1 - head) :]
    return name


def heartbeat(seconds_ago: int) -> str:
    return colour(RED if seconds_ago >= WORKER_QUIET_SECONDS else GREEN, f"{ago(seconds_ago)} ago")


def header(conn, coordinator_stats: dict, pool: str = None) -> list[str]:
    now = db.now(conn)
    title = f"Auto Archiver queue{f' - pool {pool}' if pool else ''}   {now:%Y-%m-%d %H:%M:%S} UTC"
    lines = [
        colour(BOLD, title) + colour(DIM, f"   refreshes every {REFRESH_SECONDS}s, p for the next pool, Ctrl+C to quit")
    ]

    beat = conn.execute(
        "SELECT extract(epoch FROM now() - beat_at)::int AS seconds_ago FROM heartbeats WHERE name = 'coordinator'"
    ).fetchone()
    when = colour(RED, "no heartbeat ever") if beat is None else f"heartbeat {heartbeat(beat['seconds_ago'])}"
    lines.append(f"Coordinator: {when}, {machine(coordinator_stats)}")

    if pool is None:
        q = db.get_queue_summary(conn)
        lines.append(
            f"Queue: {q['queued']} queued, {q['running']} running, "
            f"{colour(YELLOW if q['unwritten'] else GREEN, q['unwritten'])} sheet writes to do, "
            f"{colour(RED if q['unsent_alerts'] else GREEN, q['unsent_alerts'])} unsent alerts"
        )
    else:
        # alerts aren't linked to a pool, so they're left out
        q = conn.execute(
            f"""
            SELECT count(*) FILTER (WHERE j.status = 'queued') AS queued,
                count(*) FILTER (WHERE j.status = 'running') AS running,
                (SELECT count(*) FROM sheet_writes w JOIN jobs j ON j.id = w.job_id JOIN sheets s ON s.id = j.sheet_id
                 WHERE w.written_at IS NULL AND w.discarded_at IS NULL AND {IN_POOL}) AS unwritten
            FROM jobs j JOIN sheets s ON s.id = j.sheet_id WHERE {IN_POOL}
            """,
            {"pool": pool},
        ).fetchone()
        lines.append(
            f"Queue: {q['queued']} queued, {q['running']} running, "
            f"{colour(YELLOW if q['unwritten'] else GREEN, q['unwritten'])} sheet writes to do"
        )

    speed = conn.execute(
        f"""
        SELECT count(*) FILTER (WHERE j.finished_at > now() - interval '1 hour') AS hour,
            count(*) FILTER (WHERE j.finished_at > now() - interval '1 day') AS day,
            (SELECT extract(epoch FROM now() - min(j.added_at))::int FROM jobs j JOIN sheets s ON s.id = j.sheet_id
             WHERE j.status = 'queued' AND {IN_POOL}) AS oldest
        FROM jobs j JOIN sheets s ON s.id = j.sheet_id WHERE j.status IN ('done', 'failed') AND {IN_POOL}
        """,
        {"pool": pool},
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


def workers(conn, coordinator_commit: str, pool: str = None) -> list[str]:
    """pool: only that pool's workers and the shared pool's, and only that pool's jobs and counts"""
    lines = [colour(BOLD, "Workers")]
    running = {job["worker"]: job for job in db.get_running_jobs(conn)}
    # a shared worker's counts in a pool's view are only that pool's jobs
    today = {
        row["worker"]: row
        for row in conn.execute(
            f"""
            SELECT j.worker, count(*) FILTER (WHERE j.status = 'done') AS done,
                count(*) FILTER (WHERE j.status = 'failed') AS failed
            FROM jobs j JOIN sheets s ON s.id = j.sheet_id
            WHERE j.finished_at >= date_trunc('day', now()) AND {IN_POOL} GROUP BY j.worker
            """,
            {"pool": pool},
        )
    }
    beats = [beat for beat in db.get_worker_heartbeats(conn) if pool is None or beat["pool"] == pool or beat["shared"]]
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
        its_pool = colour(GREEN, beat["pool"]) if beat["pool"] else colour(RED, "none, so it claims nothing")
        if beat["shared"]:
            its_pool += colour(DIM, " (shared, helps any full pool)")
        lines.append(
            f"  {name:<{WORKER_WIDTH}} heartbeat {heartbeat(beat['seconds_ago'])}, pool {its_pool}, code {code}, "
            f"today {colour(GREEN, counts['done'])} done, {failed} failed"
        )
        lines.append(f"{INDENT}{machine(beat)}")
        job = running.get(name)
        if job is None:
            lines.append(colour(DIM, f"{INDENT}idle"))
        elif pool is not None and job["pool"] != pool:
            lines.append(colour(DIM, f"{INDENT}busy"))  # a shared worker on another pool's job, which isn't shown
        else:
            if job["work_started_at"] is None:
                took = colour(DIM, "setting up")
            else:
                # this attempt, not started_at: that's the first claim, so it would add the time of a released attempt
                seconds = (datetime.now(timezone.utc) - job["work_started_at"]).total_seconds()
                took = colour(RED if seconds > time_limit_seconds(job["url"]) else GREEN, ago(seconds))
            lines.append(
                f"{INDENT}job {job['id']} {tab(job)} "
                f"row {job['row_number']} {job['domain']}, {took}, attempt {job['attempts']}"
            )
            if job["stage"]:  # set by the archiver as it goes, from the next worker deploy
                in_stage = ago((datetime.now(timezone.utc) - job["stage_at"]).total_seconds())
                lines.append(f"{INDENT}  {job['stage']}, {in_stage} in this stage")
    return lines


def sheets(conn, pool: str = None) -> list[str]:
    lines = [
        colour(BOLD, "Sheets")
        + colour(
            DIM,
            "   done and failed are for the last 24h; rows have a URL, blank of them have no status; "
            f"active tabs are read every {SYNC_SECONDS}s, quiet ones every {ago(QUIET_TAB_SECONDS)}, red if not for {ago(READ_LATE_SECONDS)}; "
            f"slowest is the longest gap between reads that ended in the last {READ_GAP_MINUTES}m, or since the last read",
        ),
        colour(
            DIM,
            f"  {'':<{TAB_WIDTH}} {'pool':<{POOL_WIDTH}} {'queued':>6} {'running':>7} {'done':>6} {'failed':>6}"
            f" {'rows':>6} {'blank':>6}"
            f"   {'last read':<9} {'read':<6} {'slowest':<7}",
        ),
    ]
    rows = conn.execute(
        f"""
        SELECT s.id, s.spreadsheet_name, s.worksheet, s.enabled, s.paused_until > now() AS paused, s.paused_until, o.pool,
            s.url_rows, s.blank_rows, s.read_error, extract(epoch FROM now() - s.last_read_at)::int AS read_seconds_ago,
            (SELECT max(g.gap)::int FROM (
                SELECT r.read_at, extract(epoch FROM r.read_at - lag(r.read_at) OVER (ORDER BY r.read_at)) AS gap
                FROM sheet_reads r WHERE r.sheet_id = s.id
            ) g WHERE g.read_at > now() - make_interval(mins => %(gap_minutes)s)) AS slowest_gap,
            count(j.id) FILTER (WHERE j.status = 'queued') AS queued,
            count(j.id) FILTER (WHERE j.status = 'running') AS running,
            count(j.id) FILTER (WHERE j.status = 'done' AND j.finished_at > now() - interval '1 day') AS done,
            count(j.id) FILTER (WHERE j.status = 'failed' AND j.finished_at > now() - interval '1 day') AS failed
        FROM sheets s LEFT JOIN jobs j ON j.sheet_id = s.id LEFT JOIN organisations o ON o.id = s.organisation_id
        WHERE {IN_POOL}
        GROUP BY s.id, o.pool ORDER BY s.enabled DESC, o.pool, s.id
        """,
        {"pool": pool, "gap_minutes": READ_GAP_MINUTES},
    ).fetchall()
    active = db.get_recently_active_sheet_ids(conn, ACTIVE_DAYS)
    for row in rows:
        if row["paused"]:
            state = colour(RED, f"paused until {row['paused_until']:%H:%M} UTC")
        elif row["enabled"]:
            state = colour(GREEN, "on")
        else:
            state = colour(DIM, "off")
        name = tab_column(row)
        # with no organisation, its jobs are never claimed
        pool = colour(DIM if row["pool"] else RED, (row["pool"] or "none").ljust(POOL_WIDTH))
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
        # as sync's is_due: an active tab is read every pass, a quiet one every QUIET_TAB_SECONDS
        how_often = ("active" if row["id"] in active else "quiet") if row["enabled"] else ""
        # counting the time since the last read, so a tab sync has stopped reading shows it
        slowest = ""
        if row["enabled"] and seconds is not None:
            slowest = ago(max(row["slowest_gap"] or 0, seconds))
        lines.append(
            f"  {name:<{TAB_WIDTH}} {pool} {row['queued']:>6} {row['running']:>7} {done} {failed}"
            f" {url_rows:>6} {blank_rows:>6}"
            f"   {read} {how_often:<6} {slowest:<7} {state}"
        )
        # only an enabled tab is read, so an old error on one that's off is left dim
        if row["read_error"]:
            lines.append(colour(RED if row["enabled"] else DIM, f"    {row['read_error']}"))
    return lines


def sites(conn, pool: str = None) -> list[str]:
    lines = [
        colour(BOLD, "Sites") + colour(DIM, f"   the {SITES} busiest in the last 24h"),
        colour(DIM, f"  {'':<30} {'done':>6} {'failed':>6} {'blocked':>7}   {'average':<8}"),
    ]
    rows = conn.execute(
        f"""
        SELECT j.domain,
            count(*) FILTER (WHERE j.status = 'done' AND NOT w.blocked) AS done,
            count(*) FILTER (WHERE j.status = 'failed' AND NOT w.blocked) AS failed,
            count(*) FILTER (WHERE w.blocked) AS blocked,
            extract(epoch FROM avg(j.finished_at - j.work_started_at))::int AS average
        FROM jobs j JOIN sheets s ON s.id = j.sheet_id
        LEFT JOIN LATERAL (
            SELECT coalesce(cells->>'status' ILIKE '%%blocked%%', false) AS blocked
            FROM sheet_writes WHERE job_id = j.id ORDER BY id DESC LIMIT 1
        ) w ON true
        WHERE j.status IN ('done', 'failed') AND j.finished_at > now() - interval '1 day' AND {IN_POOL}
        GROUP BY j.domain ORDER BY count(*) DESC, j.domain LIMIT %(limit)s
        """,
        {"pool": pool, "limit": SITES},
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


def storage(conn, pool: str = None) -> list[str]:
    files = conn.execute(
        "SELECT count(*) AS files, coalesce(sum(f.bytes), 0) AS bytes "
        "FROM job_files f JOIN jobs j ON j.id = f.job_id JOIN sheets s ON s.id = j.sheet_id "
        f"WHERE f.created_at > now() - interval '1 day' AND {IN_POOL}",
        {"pool": pool},
    ).fetchone()
    broken = conn.execute(
        "SELECT j.id, j.store_failures FROM jobs j JOIN sheets s ON s.id = j.sheet_id "
        f"WHERE j.store_failures > 0 AND j.finished_at > now() - interval '1 day' AND {IN_POOL} ORDER BY j.id DESC",
        {"pool": pool},
    ).fetchall()
    line = f"Storage (24h): {files['files']} files, {files['bytes'] / GB:.2f} GB stored"
    if broken:
        jobs = ", ".join(f"job {row['id']} ({row['store_failures']})" for row in broken[:FAILURES])
        line += ", " + colour(RED, f"{len(broken)} job(s) with files that couldn't be stored: {jobs}")
    else:
        line += ", " + colour(GREEN, "every file stored")
    lines = [line]
    tabs = conn.execute(
        f"""
        SELECT s.spreadsheet_name, s.worksheet, t.files, t.bytes
        FROM sheets s JOIN (
            SELECT j.sheet_id, count(*) AS files, sum(f.bytes) AS bytes
            FROM job_files f JOIN jobs j ON j.id = f.job_id
            WHERE f.created_at > now() - interval '1 day' GROUP BY j.sheet_id
        ) t ON t.sheet_id = s.id
        WHERE {IN_POOL}
        -- only the tabs that stored something, as there are too many to list them all
        ORDER BY t.bytes DESC, s.id
        """,
        {"pool": pool},
    ).fetchall()
    for row in tabs:
        lines.append(f"  {tab_column(row):<{TAB_WIDTH}} {row['files']:>6} files {row['bytes'] / GB:>7.2f} GB")
    return lines


FINISHED_JOBS = """
    SELECT j.id, j.status, j.worker, j.row_number, j.domain, j.started_at, j.finished_at, j.error,
        s.spreadsheet_name, s.worksheet, w.cells->>'status' AS sheet_status
    FROM jobs j JOIN sheets s ON s.id = j.sheet_id
    LEFT JOIN LATERAL (
        SELECT cells FROM sheet_writes WHERE job_id = j.id ORDER BY id DESC LIMIT 1
    ) w ON true
    WHERE j.status IN ('done', 'failed') AND {in_pool} {where}
    ORDER BY j.finished_at DESC LIMIT %(limit)s
"""


def is_ok(row) -> bool:
    return row["status"] == "done" and "blocked" not in (row["sheet_status"] or "")


def job_line(row) -> str:
    took = ago((row["finished_at"] - row["started_at"]).total_seconds()) if row["started_at"] else "?"
    result = colour(GREEN if is_ok(row) else YELLOW, (row["sheet_status"] or row["status"])[:80])
    return (
        f"  {row['finished_at']:%Y-%m-%d %H:%M} {row['worker'] or '':<{WORKER_WIDTH}} job {row['id']} "
        f"{tab(row)} row {row['row_number']} "
        f"{row['domain']}, {took}: {result}"
    )


def recent(conn, pool: str = None) -> list[str]:
    rows = conn.execute(
        FINISHED_JOBS.format(in_pool=IN_POOL, where=""), {"pool": pool, "limit": RECENT_JOBS}
    ).fetchall()
    return [colour(BOLD, f"Last {RECENT_JOBS} finished")] + [job_line(row) for row in rows]


def failures(conn, pool: str = None) -> list[str]:
    where = "AND (j.status = 'failed' OR w.cells->>'status' ILIKE '%%blocked%%')"
    rows = conn.execute(
        FINISHED_JOBS.format(in_pool=IN_POOL, where=where), {"pool": pool, "limit": FAILURES}
    ).fetchall()
    lines = [colour(BOLD, f"Last {FAILURES} failed or blocked")]
    if not rows:
        lines.append(colour(GREEN, "  none"))
    return lines + [job_line(row) for row in rows]


def waiting(conn, pool: str = None) -> list[str]:
    """Queued jobs that can't be claimed right now, and why"""
    rows = conn.execute(
        f"""
        SELECT j.id, j.row_number, j.domain, j.attempts, j.available_at, s.spreadsheet_name, s.worksheet, s.setup_error,
            CASE
                WHEN NOT s.enabled THEN 'sheet is off'
                WHEN s.organisation_id IS NULL THEN 'sheet has no organisation, so no pool'
                WHEN NOT EXISTS (SELECT 1 FROM organisations o JOIN workers w ON w.pool = o.pool
                                 WHERE o.id = s.organisation_id) THEN 'no worker in its pool'
                WHEN s.paused_until > now() THEN 'sheet is paused'
                WHEN j.available_at > now() THEN 'retry'
                WHEN NOT {db.SITE_HAS_ROOM} THEN 'site busy'
                -- eg the glan pool's workers are busy while the cir workers are idle. The same test as claim_job
                WHEN NOT EXISTS (SELECT 1 FROM organisations o WHERE o.id = s.organisation_id AND EXISTS ({db.IDLE_WORKER}))
                    THEN 'every worker in its pool is busy or down'
            END AS reason
        FROM jobs j JOIN sheets s ON s.id = j.sheet_id
        WHERE j.status = 'queued' AND {IN_POOL}
        ORDER BY j.id
        """,
        {"pool": pool, "down_seconds": db.WORKER_DOWN_SECONDS},
    ).fetchall()
    held = [row for row in rows if row["reason"]]
    lines = [colour(BOLD, "Waiting") + colour(DIM, f"   {len(held)} queued job(s) that can't be claimed yet")]
    for row in held[:WAITING_JOBS]:
        reason = row["reason"]
        if reason == "retry":
            reason = f"retry at {row['available_at']:%H:%M} UTC, attempt {row['attempts'] + 1}"
        elif reason == "sheet is paused" and row["setup_error"]:
            reason = f"sheet is paused, its jobs fail in setup: {row['setup_error']}"
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


def render(conn, coordinator_commit: str = "?", coordinator_stats: dict = None, pool: str = None) -> str:
    """pool: only that pool's sheets, jobs and workers, and no alerts, as they aren't linked to a pool"""
    sections = [
        header(conn, coordinator_stats or {}, pool),
        workers(conn, coordinator_commit, pool),
        sheets(conn, pool),
        waiting(conn, pool),
        sites(conn, pool),
        storage(conn, pool),
        failures(conn, pool),
        recent(conn, pool),
    ]
    if pool is None:
        sections.append(alerts(conn))
    return "\n\n".join("\n".join(section) for section in sections)


def next_pool(pool: str, pools: list[str]) -> str:
    """All (None), then each pool in turn, then all again"""
    order = [None, *pools]
    return order[(order.index(pool) + 1) % len(order)] if pool in order else None


def read_key(seconds: float) -> str:
    """A key pressed within seconds, or '' if none"""
    ready, _, _ = select.select([sys.stdin], [], [], seconds)
    return sys.stdin.read(1) if ready else ""


def main() -> None:
    pool, pools = None, []
    # keys are read as they're pressed, without Enter. Ctrl+C still stops it
    terminal = termios.tcgetattr(sys.stdin) if sys.stdin.isatty() else None
    if terminal:
        tty.setcbreak(sys.stdin)
    try:
        while True:
            # a new connection each time, so a Postgres restart only loses one refresh
            try:
                with db.connect() as conn:
                    # only the pools with workers, eg not an unused one
                    pools = [row["pool"] for row in conn.execute("SELECT DISTINCT pool FROM workers ORDER BY pool")]
                    # the dashboard runs on the coordinator, from its repo, so these are the coordinator's, to
                    # compare the workers' code with. Each time, so it shows a deploy without restarting the dashboard
                    screen = render(conn, git_commit(), machine_stats("."), pool)
            except Exception as e:
                screen = colour(RED, f"Can't read the queue database: {e}")
            print(HOME + screen.replace("\n", CLEAR_LINE + "\n") + CLEAR_LINE + CLEAR_BELOW, end="", flush=True)
            try:
                key = read_key(REFRESH_SECONDS) if terminal else time.sleep(REFRESH_SECONDS)
            except KeyboardInterrupt:
                print()
                return
            if key == "p":
                pool = next_pool(pool, pools)
    finally:
        if terminal:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, terminal)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
