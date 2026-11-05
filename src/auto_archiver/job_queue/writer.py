"""
Sheet writer: sends the waiting sheet writes to the sheets, and is the only part that writes to them. See queue-spec.md

Only each job's latest entry is sent, as it holds the row's current status. Before writing, it re-reads the row's
URL: if the URL has changed, the rows have moved, so it drops the job's entries rather than write to the wrong row.
The status cell belongs to the archiver and is overwritten. The results stay in Postgres either way.

    python -m auto_archiver.job_queue.writer run [--once]
"""

import argparse
import time

from gspread import utils as gspread_utils

from auto_archiver.job_queue import db
from auto_archiver.job_queue.db import describe_tab
from auto_archiver.job_queue.sync import load_settings, log_on_change, open_spreadsheet
from auto_archiver.modules.gsheet_feeder_db.gsheet_feeder_db import cells_to_write
from auto_archiver.utils.custom_logger import logger

READ_WRITE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
WRITER_INTERVAL_SECONDS = 30
# Sheets' limit for a cell
MAX_CELL_LENGTH = 49999


def open_for_writing(settings: dict, spreadsheet_id: str = None):
    return open_spreadsheet(settings, spreadsheet_id, READ_WRITE_SCOPES)


def _cell(value_range: dict) -> str:
    values = value_range.get("values") or [[]]
    return values[0][0] if values[0] else ""


def _letter(position: int) -> str:
    return gspread_utils.rowcol_to_a1(1, position)[:-1]


def _log_written(entry: dict, spreadsheet_name: str, tab_title: str, changed: list[str]) -> None:
    """changed: the cells that had a different value, which were overwritten"""
    status = entry["cells"].get("status", "")
    log = logger.warning if status.startswith("Archive failed") else logger.info
    replaced = f", replacing {len(changed)} changed cell(s): {', '.join(changed)}" if changed else ""
    log(
        f"{describe_tab(spreadsheet_name, tab_title)} row {entry['row_number']}, job {entry['job_id']}: "
        f"wrote {status!r}{replaced}"
    )


def _discard(conn, entry: dict, spreadsheet_name: str, tab_title: str, reason: str) -> None:
    message = (
        f"{describe_tab(spreadsheet_name, tab_title)} row {entry['row_number']}, job {entry['job_id']}: {reason}, "
        "so its sheet writes were dropped. The result is still in Postgres"
    )
    logger.warning(message)
    db.discard_job_writes(conn, entry["job_id"], message)


def _write_tab(conn, tab, entries: list[dict], settings: dict, read_ranges, spreadsheet_name: str) -> int:
    """Checks then writes one tab's entries, with one read for the headers, one for the rows and one write"""
    header, columns = settings["header"], settings["columns"]
    names = [str(name).lower() for name in (read_ranges([f"{header}:{header}"], tab)[0].get("values") or [[]])[0]]
    positions = {col: names.index(name.lower()) + 1 for col, name in columns.items() if name.lower() in names}

    if "url" not in positions or "status" not in positions:
        # probably a header being renamed, so the entries wait and are tried again next cycle
        log_on_change(
            (tab.id, "writer columns"),
            f"{describe_tab(spreadsheet_name, tab.title)}: not writing, as the tab has no url or status column",
        )
        return 0

    # each entry's URL and the cells it wants to write, and no others, as cells can hold 50,000 characters
    ranges, wanted = [], []
    for entry in entries:
        cols = ["url"] + [c for c in entry["cells"] if c in positions and c != "url"]
        wanted.append(cols)
        ranges.extend(f"{_letter(positions[c])}{entry['row_number']}" for c in cols)
    results = iter(read_ranges(ranges, tab))

    updates, written, logged = [], [], []
    for entry, cols in zip(entries, wanted):
        current = {col: _cell(next(results)) for col in cols}
        if not entry["skip_url_check"] and current["url"].strip() != entry["url"].strip():
            _discard(conn, entry, spreadsheet_name, tab.title, "the URL has changed")
            continue
        written.append(entry["id"])
        to_write = cells_to_write(
            entry["cells"],
            lambda col: col in positions,
            lambda col: current.get(col, ""),
            settings["allow_overwrite"],
            log_overwrites=False,
        )
        changed = [c for c, v in to_write.items() if c != "status" and current.get(c, "") not in ("", str(v))]
        logged.append((entry, changed))
        for col, val in to_write.items():
            if len(str(val)) > MAX_CELL_LENGTH:
                logger.warning(
                    f"{describe_tab(spreadsheet_name, tab.title)} row {entry['row_number']}, job {entry['job_id']}: "
                    f"truncating {col} from {len(str(val))} characters"
                )
            updates.append(
                {"range": f"{_letter(positions[col])}{entry['row_number']}", "values": [[str(val)[:MAX_CELL_LENGTH]]]}
            )

    if updates:
        tab.batch_update(updates, value_input_option="USER_ENTERED")
    if written:
        db.mark_written(conn, written)
    for entry, changed in logged:
        _log_written(entry, spreadsheet_name, tab.title, changed)
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
                _discard(conn, entry, spreadsheet.title, str(gid), "the tab has been deleted")
            continue
        written += _write_tab(conn, tabs[gid], tab_entries, settings, read_ranges, spreadsheet.title)
    return written


def writer_cycle(conn, opener=open_for_writing) -> int:
    """Each job's latest entry. A failure only loses that spreadsheet's turn, and it's tried again next time"""
    by_spreadsheet = {}
    for entry in db.get_next_sheet_writes(conn):
        by_spreadsheet.setdefault((entry["spreadsheet_id"], entry["config"]), []).append(entry)

    written = 0
    for (spreadsheet_id, config_path), entries in by_spreadsheet.items():
        try:
            written += _write_spreadsheet(conn, spreadsheet_id, config_path, entries, opener)
        except Exception as e:
            logger.error(f"Writing to the sheet for config {config_path} failed: {e}")
    db.beat(conn, "writer")
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Sheet writer for the job queue")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="send the waiting sheet writes to the sheets")
    run_parser.add_argument("--once", action="store_true", help="one cycle, then exit")
    args = parser.parse_args()

    while True:
        # a new connection each time, so a Postgres restart only loses one cycle
        try:
            with db.connect() as conn:
                logger.info(f"Wrote {writer_cycle(conn)} sheet writes")
        except Exception as e:
            logger.error(f"Writer failed: {e}")
        if args.once:
            return
        time.sleep(WRITER_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
