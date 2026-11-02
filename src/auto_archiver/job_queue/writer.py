"""
Sheet writer: sends the outbox to the sheets, and is the only part that writes to them. See queue-spec.md

Before each write it re-reads the row. If the URL or status isn't what's expected it writes nothing and records a
conflict, as a client may have changed the sheet. The results stay in Postgres, so nothing needs archiving again.

    python -m auto_archiver.job_queue.writer run [--once]
    python -m auto_archiver.job_queue.writer conflicts
    python -m auto_archiver.job_queue.writer retry <outbox id>
    python -m auto_archiver.job_queue.writer discard <outbox id>
"""

import argparse
import time

from gspread import utils as gspread_utils

from auto_archiver.job_queue import db
from auto_archiver.job_queue.sync import load_settings, open_spreadsheet
from auto_archiver.modules.gsheet_feeder_db.gsheet_feeder_db import cells_to_write
from auto_archiver.utils.custom_logger import logger

READ_WRITE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
WRITER_INTERVAL_SECONDS = 30
# Sheets' limit for a cell
MAX_CELL_LENGTH = 49999
# an entry at a time per job, so a job can go from Queued through to its result in one cycle
MAX_PASSES = 5


def open_for_writing(settings: dict, spreadsheet_id: str = None):
    return open_spreadsheet(settings, spreadsheet_id, READ_WRITE_SCOPES)


def _cell(value_range: dict) -> str:
    values = value_range.get("values") or [[]]
    return values[0][0] if values[0] else ""


def _letter(position: int) -> str:
    return gspread_utils.rowcol_to_a1(1, position)[:-1]


def _check_row(entry: dict, current_url: str, current_status: str) -> tuple[bool, str | None]:
    """The sanity check: returns (already written, conflict)"""
    url_ok = entry["skip_url_check"] or current_url == entry["url"].strip()
    # the writer stopped after updating the sheet last time, before it could record that
    if url_ok and current_status == entry["cells"].get("status") != entry["expected_status"]:
        return True, None
    if not url_ok:
        return False, f"Job {entry['job_id']} row {entry['row_number']}: the URL has changed"
    if current_status != entry["expected_status"]:
        return (
            False,
            f"Job {entry['job_id']} row {entry['row_number']}: "
            f"status is {current_status!r}, expected {entry['expected_status']!r}",
        )
    return False, None


def _log_written(entry: dict) -> None:
    status = entry["cells"].get("status", "")
    log = logger.warning if status.startswith("Archive failed") else logger.info
    log(f"Job {entry['job_id']} row {entry['row_number']}: wrote {status!r}")


def _write_tab(conn, tab, entries: list[dict], settings: dict, read_ranges) -> int:
    """Checks then writes one tab's entries, with one read for the headers, one for the rows and one write"""
    header, columns = settings["header"], settings["columns"]
    names = [str(name).lower() for name in (read_ranges([f"{header}:{header}"], tab)[0].get("values") or [[]])[0]]
    positions = {col: names.index(name.lower()) + 1 for col, name in columns.items() if name.lower() in names}

    if "url" not in positions or "status" not in positions:
        for entry in entries:
            db.set_conflict(conn, entry["id"], f"Job {entry['job_id']}: the tab has no url or status column")
        return 0

    # each entry's URL, status and the cells it wants to write, and no others, as cells can hold 50,000 characters
    ranges, wanted = [], []
    for entry in entries:
        cols = ["url", "status"] + [c for c in entry["cells"] if c in positions and c not in ("url", "status")]
        wanted.append(cols)
        ranges.extend(f"{_letter(positions[c])}{entry['row_number']}" for c in cols)
    results = iter(read_ranges(ranges, tab))

    updates, written, logged = [], [], []
    for entry, cols in zip(entries, wanted):
        current = {col: _cell(next(results)) for col in cols}
        already_written, conflict = _check_row(entry, current["url"].strip(), current["status"])
        if conflict:
            db.set_conflict(conn, entry["id"], conflict)
            continue
        written.append(entry["id"])
        if already_written:
            continue
        logged.append(entry)
        to_write = cells_to_write(
            entry["cells"],
            lambda col: col in positions,
            lambda col: current.get(col, ""),
            settings["allow_overwrite"],
        )
        for col, val in to_write.items():
            if len(str(val)) > MAX_CELL_LENGTH:
                logger.warning(f"Truncating {col} in job {entry['job_id']} from {len(str(val))} characters")
            updates.append(
                {"range": f"{_letter(positions[col])}{entry['row_number']}", "values": [[str(val)[:MAX_CELL_LENGTH]]]}
            )

    if updates:
        tab.batch_update(updates, value_input_option="USER_ENTERED")
    if written:
        db.mark_written(conn, written)
    for entry in logged:
        _log_written(entry)
    return len(written)


def _write_spreadsheet(conn, spreadsheet_id: str, config_path: str, entries: list[dict], opener) -> int:
    settings = load_settings(config_path)
    spreadsheet = opener(settings, spreadsheet_id)
    # by gid, so a rename doesn't matter, including one that takes the tab off the config's allow list
    tabs = {tab.id: tab for tab in spreadsheet.worksheets()}

    def read_ranges(ranges, tab):
        full = [gspread_utils.absolute_range_name(tab.title, r) for r in ranges]
        return spreadsheet.values_batch_get(full)["valueRanges"]

    by_tab = {}
    for entry in entries:
        by_tab.setdefault(entry["worksheet_gid"], []).append(entry)

    written = 0
    for gid, tab_entries in by_tab.items():
        if gid not in tabs:
            for entry in tab_entries:
                db.set_conflict(conn, entry["id"], f"Job {entry['job_id']}: the tab has been deleted")
            continue
        written += _write_tab(conn, tabs[gid], tab_entries, settings, read_ranges)
    return written


def writer_pass(conn, opener=open_for_writing) -> int:
    """Each job's next entry. A failure only loses that spreadsheet's turn, and it's tried again next time"""
    by_spreadsheet = {}
    for entry in db.get_next_outbox_entries(conn):
        by_spreadsheet.setdefault((entry["spreadsheet_id"], entry["config"]), []).append(entry)

    written = 0
    for (spreadsheet_id, config_path), entries in by_spreadsheet.items():
        try:
            written += _write_spreadsheet(conn, spreadsheet_id, config_path, entries, opener)
        except Exception as e:
            logger.error(f"Writing to the sheet for config {config_path} failed: {e}")
    return written


def writer_cycle(conn, opener=open_for_writing) -> int:
    written = 0
    for _ in range(MAX_PASSES):
        count = writer_pass(conn, opener)
        written += count
        if count == 0:
            break
    db.beat(conn, "writer")
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Sheet writer for the job queue")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="send the outbox to the sheets")
    run_parser.add_argument("--once", action="store_true", help="one cycle, then exit")
    commands.add_parser("conflicts", help="list the entries waiting on a conflict")
    commands.add_parser("retry", help="check a conflicted entry again").add_argument("outbox_id", type=int)
    commands.add_parser("discard", help="give up on a conflicted entry").add_argument("outbox_id", type=int)
    args = parser.parse_args()

    if args.command == "conflicts":
        with db.connect() as conn:
            for conflict in db.get_conflicts(conn):
                print(f"{conflict['id']}: sheet {conflict['sheet_id']}: {conflict['conflict']}")
        return
    if args.command in ("retry", "discard"):
        action = db.retry_conflict if args.command == "retry" else db.discard_conflict
        with db.connect() as conn:
            print("Done" if action(conn, args.outbox_id) else f"No conflict on entry {args.outbox_id}")
        return

    while True:
        # a new connection each time, so a Postgres restart only loses one cycle
        try:
            with db.connect() as conn:
                logger.info(f"Wrote {writer_cycle(conn)} outbox entries")
        except Exception as e:
            logger.error(f"Writer failed: {e}")
        if args.once:
            return
        time.sleep(WRITER_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
