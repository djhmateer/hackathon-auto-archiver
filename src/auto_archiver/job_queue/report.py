"""
The morning report: what needs attention from the last 24 hours, then the detail behind it. Run on the coordinator by
cron at 05:00 UTC (deploy-coordinator.sh adds the line), before the workers' 05:20 reboot. It's emailed with the alert
settings in .env (see alerts.py), saved to ~/reports/report_<date>.txt and printed.

Read only. Aggregates only: sheets, domains, counts and job ids, never URLs or row contents, as it goes by email.

    poetry run python -m auto_archiver.job_queue.report [--hours 24] [--no-email]
"""

import argparse
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path

from auto_archiver.job_queue import alerts, db, dashboard
from auto_archiver.job_queue.dashboard import GB, ago
from auto_archiver.job_queue.worker_supervisor import git_commit, machine_stats
from auto_archiver.utils.custom_logger import logger

REPORT_DIR = Path.home() / "reports"
NOT_READ_SECONDS = 5 * 60  # sync reads every tab that's on every 15s
QUEUED_TOO_LONG_SECONDS = 6 * 60 * 60
UNWRITTEN_TOO_LONG_SECONDS = 10 * 60
HEARTBEAT_LATE_SECONDS = 60
LISTED = 10  # job ids listed in a problem, at most


def outcome(job: dict) -> str:
    """done, failed, or blocked when the site blocked the archiver, whichever way the job ended"""
    if "blocked" in (job["sheet_status"] or ""):
        return "blocked"
    return job["status"]


OUTCOMES = ("done", "failed", "blocked")


def seconds(job: dict) -> float | None:
    # from the first claim, so a retried job's time includes its earlier attempts
    return (job["finished_at"] - job["started_at"]).total_seconds() if job["started_at"] else None


def ids(jobs: list[dict]) -> str:
    listed = ", ".join(str(job["id"]) for job in jobs[:LISTED])
    return listed + (f" and {len(jobs) - LISTED} more" if len(jobs) > LISTED else "")


def counts(items) -> str:
    """eg idf.il 2, reuters.com 1, busiest first"""
    return ", ".join(f"{name} {n}" for name, n in Counter(items).most_common())


def table(headers: list[str], rows: list[list]) -> list[str]:
    """Plain text columns: the first left aligned, the rest right aligned"""
    cells = [headers] + [[str(cell) for cell in row] for row in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
    return [
        "  " + "  ".join(cell.ljust(w) if i == 0 else cell.rjust(w) for i, (cell, w) in enumerate(zip(row, widths)))
        for row in cells
    ]


def finished_jobs(conn, hours: int) -> list[dict]:
    return conn.execute(
        """
        SELECT j.id, j.status, j.domain, j.worker, j.started_at, j.finished_at, j.error, j.store_failures,
            s.spreadsheet_name, s.worksheet, w.cells->>'status' AS sheet_status
        FROM jobs j JOIN sheets s ON s.id = j.sheet_id
        LEFT JOIN LATERAL (
            SELECT cells FROM sheet_writes WHERE job_id = j.id ORDER BY id DESC LIMIT 1
        ) w ON true
        WHERE j.status IN ('done', 'failed') AND j.finished_at > now() - %s * interval '1 hour'
        ORDER BY j.id
        """,
        (hours,),
    ).fetchall()


def problems(conn, jobs: list[dict], hours: int, coordinator_commit: str, coordinator_stats: dict) -> list[str]:
    """Each one is a sentence. None means nothing needs attention"""
    found = []

    killed = [job for job in jobs if "still running after" in (job["error"] or "")]
    failed = [job for job in jobs if outcome(job) == "failed" and job not in killed]
    blocked = [job for job in jobs if outcome(job) == "blocked"]
    unstored = [job for job in jobs if job["store_failures"]]
    if failed:
        found.append(f"{len(failed)} job(s) failed ({counts(j['domain'] for j in failed)}): jobs {ids(failed)}")
    if killed:
        found.append(f"{len(killed)} job(s) killed at the time limit: jobs {ids(killed)}")
    if blocked:
        found.append(f"{len(blocked)} job(s) blocked by the site ({counts(j['domain'] for j in blocked)})")
    if unstored:
        found.append(f"{len(unstored)} job(s) with files that couldn't be stored: jobs {ids(unstored)}")

    raised = conn.execute(
        "SELECT kind FROM admin_alerts WHERE created_at > now() - %s * interval '1 hour'", (hours,)
    ).fetchall()
    if raised:
        found.append(f"Alerts raised: {counts(a['kind'] for a in raised)} (listed below)")
    unsent = conn.execute(
        "SELECT count(*) AS n FROM admin_alerts WHERE sent_at IS NULL AND kind = ANY(%s)", (list(alerts.EMAIL_KINDS),)
    ).fetchone()["n"]
    if unsent:
        found.append(f"{unsent} alert(s) that should have been emailed are still unsent")

    unwritten = conn.execute(
        """
        SELECT count(*) AS n, extract(epoch FROM now() - min(created_at))::int AS oldest FROM sheet_writes
        WHERE written_at IS NULL AND discarded_at IS NULL
        """
    ).fetchone()
    if unwritten["n"] and unwritten["oldest"] > UNWRITTEN_TOO_LONG_SECONDS:
        found.append(f"{unwritten['n']} sheet write(s) not sent, the oldest from {ago(unwritten['oldest'])} ago")

    queued = conn.execute(
        "SELECT count(*) AS n, extract(epoch FROM now() - min(added_at))::int AS oldest FROM jobs WHERE status = 'queued'"
    ).fetchone()
    if queued["n"] and queued["oldest"] > QUEUED_TOO_LONG_SECONDS:
        found.append(f"{queued['n']} job(s) queued, the oldest added {ago(queued['oldest'])} ago")

    now = db.now(conn)
    for sheet in db.get_sheets(conn):
        name = dashboard.tab(sheet)
        if sheet["paused_until"] and sheet["paused_until"] > now:
            found.append(f"{name} is paused until {sheet['paused_until']:%H:%M} UTC")
        if sheet["enabled"]:
            last_read = sheet["last_read_at"]
            if last_read is None or (now - last_read).total_seconds() > NOT_READ_SECONDS:
                when = "never" if last_read is None else f"not since {last_read:%Y-%m-%d %H:%M} UTC"
                found.append(f"{name} is on but sync has read it {when}")

    for beat in db.get_worker_heartbeats(conn):
        name = beat["worker"]
        if beat["seconds_ago"] > HEARTBEAT_LATE_SECONDS:
            found.append(f"Worker {name} has sent no heartbeat for {ago(beat['seconds_ago'])}")
            continue
        if beat["git_commit"] and beat["git_commit"] != coordinator_commit:
            found.append(f"Worker {name} runs code {beat['git_commit']}, the coordinator {coordinator_commit}")
        found += machine_problems(f"Worker {name}", beat)
    found += machine_problems("The coordinator", coordinator_stats)

    backups = sorted(dashboard.BACKUP_DIR.glob("auto_archiver_*.dump"), key=lambda p: p.stat().st_mtime)
    if not backups:
        found.append(f"No database backup in {dashboard.BACKUP_DIR}")
    elif time.time() - backups[-1].stat().st_mtime > dashboard.BACKUP_LATE_SECONDS:
        found.append(f"The last database backup is from {ago(time.time() - backups[-1].stat().st_mtime)} ago")
    return found


def machine_problems(name: str, stats: dict) -> list[str]:
    found = []
    if stats.get("disk_free_bytes") is not None and stats["disk_free_bytes"] < dashboard.DISK_LOW:
        found.append(f"{name} has {stats['disk_free_bytes'] / GB:.1f} GB of disk free")
    if (
        stats.get("mem_total_bytes")
        and stats["mem_available_bytes"] / stats["mem_total_bytes"] < dashboard.MEMORY_VERY_LOW
    ):
        found.append(f"{name} has {stats['mem_available_bytes'] / GB:.1f} GB of memory available")
    if stats.get("swap_used_bytes") is not None and stats["swap_used_bytes"] > dashboard.SWAP_HIGH:
        found.append(f"{name} is using {stats['swap_used_bytes'] / GB:.1f} GB of swap")
    return found


def detail(conn, jobs: list[dict], hours: int) -> list[str]:
    def outcome_counts(group: list[dict]) -> list[int]:
        found = Counter(map(outcome, group))
        return [found[name] for name in OUTCOMES]

    done, failed, blocked = outcome_counts(jobs)
    lines = [f"{len(jobs)} job(s) finished: {done} done, {failed} failed, {blocked} blocked by the site"]

    by_sheet = defaultdict(list)
    for job in jobs:
        by_sheet[dashboard.tab(job)].append(job)
    queued = {
        dashboard.tab(row): row["n"]
        for row in conn.execute(
            """
            SELECT s.spreadsheet_name, s.worksheet, count(*) AS n FROM jobs j JOIN sheets s ON s.id = j.sheet_id
            WHERE j.status = 'queued' GROUP BY s.id
            """
        )
    }
    names = sorted(set(by_sheet) | set(queued))
    lines += ["", "Sheets"] + table(
        ["", "done", "failed", "blocked", "queued now"],
        [[name, *outcome_counts(by_sheet[name]), queued.get(name, 0)] for name in names],
    )

    by_site = defaultdict(list)
    for job in jobs:
        by_site[job["domain"]].append(job)
    site_rows = []
    for domain, group in sorted(by_site.items(), key=lambda item: -len(item[1])):
        times = sorted(t for t in map(seconds, group) if t is not None)
        median = ago(statistics.median(times)) if times else "?"
        # the slowest 5%, or the slowest job when there are too few for that to mean much
        p95 = ago(statistics.quantiles(times, n=20)[-1]) if len(times) >= 20 else ago(times[-1]) if times else "?"
        site_rows.append([domain, *outcome_counts(group), median, p95])
    lines += ["", "Sites"] + table(["", "done", "failed", "blocked", "median", "p95"], site_rows)

    by_worker = defaultdict(list)
    for job in jobs:
        by_worker[job["worker"] or "?"].append(job)
    worker_rows = []
    for worker, group in sorted(by_worker.items()):
        times = [t for t in map(seconds, group) if t is not None]
        worker_rows.append([worker, len(group), ago(sum(times)), ago(max(times)) if times else "?"])
    lines += ["", "Workers"] + table(["", "jobs", "busy", "longest"], worker_rows)

    stored = conn.execute(
        """
        SELECT s.client, s.spreadsheet_name, s.worksheet, count(*) AS files, coalesce(sum(f.bytes), 0) AS bytes
        FROM job_files f JOIN jobs j ON j.id = f.job_id JOIN sheets s ON s.id = j.sheet_id
        WHERE f.created_at > now() - %s * interval '1 hour'
        GROUP BY s.id ORDER BY s.client, s.id
        """,
        (hours,),
    ).fetchall()
    lines += ["", "Storage"] + table(
        ["", "client", "files", "GB"],
        [[dashboard.tab(row), row["client"] or "-", row["files"], f"{row['bytes'] / GB:.2f}"] for row in stored],
    )

    per_hour = Counter(f"{job['finished_at']:%H}" for job in jobs)
    lines += ["", "Jobs finished per hour (UTC)", "  " + ", ".join(f"{h}h {per_hour[h]}" for h in sorted(per_hour))]

    raised = conn.execute(
        "SELECT * FROM admin_alerts WHERE created_at > now() - %s * interval '1 hour' ORDER BY id", (hours,)
    ).fetchall()
    lines += ["", "Alerts"]
    # the first line only: later lines can hold the row's URL
    lines += [f"  {a['created_at']:%m-%d %H:%M} {a['kind']}: {a['message'].splitlines()[0][:150]}" for a in raised]
    if not raised:
        lines.append("  none")
    return lines


def build(conn, hours: int, coordinator_commit: str, coordinator_stats: dict) -> tuple[str, str]:
    """The email's subject and body"""
    now = db.now(conn)
    jobs = finished_jobs(conn, hours)
    found = problems(conn, jobs, hours, coordinator_commit, coordinator_stats)
    subject = f"Auto Archiver report {now:%Y-%m-%d}: " + (
        f"{len(found)} problem(s)" if found else "nothing needs attention"
    )
    lines = [f"The {hours} hours to {now:%Y-%m-%d %H:%M} UTC, code {coordinator_commit}", "", "Problems"]
    lines += [f"- {problem}" for problem in found] or ["  none"]
    lines += [""] + detail(conn, jobs, hours)
    return subject, "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="The morning report")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--no-email", action="store_true", help="print and save it only")
    args = parser.parse_args()

    with db.connect() as conn:
        subject, body = build(conn, args.hours, git_commit(), machine_stats("."))
    print(f"{subject}\n\n{body}")
    REPORT_DIR.mkdir(exist_ok=True)
    (REPORT_DIR / f"report_{time.strftime('%Y-%m-%d_%H%M', time.gmtime())}.txt").write_text(f"{subject}\n\n{body}")

    if args.no_email:
        return
    token, from_email, to = (db.env_value(n) for n in ("POSTMARK_API_TOKEN", "POSTMARK_FROM_EMAIL", "ALERT_EMAIL_TO"))
    if not (token and from_email and to):
        logger.error("Not emailed: POSTMARK_API_TOKEN, POSTMARK_FROM_EMAIL or ALERT_EMAIL_TO isn't set in .env")
        return
    alerts.send_email(token, from_email, to, subject, body)
    logger.info("Emailed the report")


if __name__ == "__main__":
    main()
