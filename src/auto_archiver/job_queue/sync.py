"""
Sheet sync: reads the sheets and works out which rows need a job. See queue-spec.md

decide() holds the sync rules with no network or database. They run on each tab with sheets.enabled on, adding jobs
and sheet writes, as the queue owns the tab. A tab that's off isn't read, as cron still owns it.

Sync only ever connects to Google read-only. The sheet writer is the only part that writes to a sheet.

    python -m auto_archiver.job_queue.sync register secrets/<config>.yaml
    python -m auto_archiver.job_queue.sync run [--once]
"""

import argparse
import json
import os
import time
from dataclasses import dataclass

import gspread
from gspread import utils as gspread_utils
from slugify import slugify

from auto_archiver.core.config import read_yaml
from auto_archiver.job_queue import db
from auto_archiver.modules.gsheet_feeder_db import GWorksheet
from auto_archiver.utils.custom_logger import logger
from auto_archiver.utils.url import sheet_url_hostname, site_for_hostname

READ_ONLY_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]
SHEETS_TIMEOUT_SECONDS = 60
SYNC_INTERVAL_SECONDS = 60
OPTIONAL_COLUMNS = ("folder",)

ADD = "add"
REJECT = "reject"
CANCEL = "cancel"
ALERT = "alert"


@dataclass
class Row:
    """A row of the sheet with a URL in it"""

    number: int
    url: str
    status: str = ""
    folder: str = ""


@dataclass
class RowState:
    """What Postgres knows about a row. job_status is only set for an active job: 'queued' or 'running'"""

    job_id: int = None
    job_status: str = None
    job_url: str = None
    has_unwritten: bool = False
    written_since_snapshot: bool = False


@dataclass
class Action:
    kind: str
    row: int
    url: str = None
    domain: str = None
    folder: str = ""
    message: str = None
    job_id: int = None


# what was last logged for each key, so a line that's the same every cycle is only logged when it changes
# the last message logged for each key, eg (spreadsheet id, tab id)
_last_logged = {}


def log_on_change(key, message: str) -> None:
    if _last_logged.get(key) != message:
        # depth=1, so the line shows the function that called this
        logger.opt(depth=1).info(message)
        _last_logged[key] = message


def tab_folder(raw_folder: str, spreadsheet_name: str, tab_title: str, use_sheet_names: bool) -> str:
    """The folder files are stored in, as the feeder works it out. No folder in the row means none at all"""
    folder = slugify((raw_folder or "").strip())
    if folder and use_sheet_names:
        return os.path.join(folder, slugify(spreadsheet_name), slugify(tab_title))
    return folder


def decide(
    rows: list[Row],
    states: dict[int, RowState],
    spreadsheet_name: str = "",
    tab_title: str = "",
    use_sheet_names: bool = False,
    must_have_folder: bool = False,
) -> list[Action]:
    """The sync rules: takes a tab's rows and what Postgres knows about them, and returns what to do"""
    actions = []
    numbers = {row.number for row in rows}

    # rule 5: a queued job whose row has gone
    for number, state in states.items():
        if number not in numbers and state.job_status == "queued":
            actions.append(Action(CANCEL, number, job_id=state.job_id))

    for row in rows:
        state = states.get(row.number, RowState())

        # rule 5: a queued job whose row has a different URL now. The next cycle adds the new URL
        if state.job_status == "queued" and state.job_url != row.url:
            actions.append(Action(CANCEL, row.number, job_id=state.job_id))
            continue

        blocked = state.job_status is not None or state.has_unwritten

        if row.status in ("", None):
            # rule 2, and rule 1's check for a result written while the sheet was being read
            if blocked or state.written_since_snapshot:
                continue
            hostname = sheet_url_hostname(row.url)
            if "." not in hostname:
                actions.append(Action(REJECT, row.number, row.url, message="WARNING: not a valid URL"))
            elif must_have_folder and not (row.folder or "").strip():
                actions.append(Action(REJECT, row.number, row.url, message="WARNING:Folder Name not set"))
            else:
                folder = tab_folder(row.folder, spreadsheet_name, tab_title, use_sheet_names)
                # the site, not the hostname, as the queue runs one job per site at a time
                actions.append(Action(ADD, row.number, row.url, domain=site_for_hostname(hostname), folder=folder))
        elif (row.status == db.STATUS_QUEUED or db.is_in_progress(row.status)) and not blocked:
            # rule 3, unless the job's last write came after this read, so the row has already moved on
            if state.written_since_snapshot:
                continue
            actions.append(Action(ALERT, row.number, row.url, message=f"Row says {row.status} but has no active job"))
        # rule 4: any other status, nothing to do

    return actions


def load_settings(config_path: str) -> dict:
    """The feeder's settings from a config file, with the same defaults as its manifest"""
    settings = read_yaml(config_path).get("gsheet_feeder_db") or {}
    columns = settings.get("columns") or GWorksheet.COLUMN_NAMES
    if isinstance(columns, str):
        columns = json.loads(columns)
    return {
        "sheet": settings.get("sheet"),
        "sheet_id": settings.get("sheet_id"),
        "header": int(settings.get("header") or 1),
        "service_account": settings.get("service_account") or "secrets/service_account.json",
        "columns": columns,
        "allow_worksheets": set(settings.get("allow_worksheets") or []),
        "block_worksheets": set(settings.get("block_worksheets") or []),
        "use_sheet_names": settings.get("use_sheet_names_in_stored_paths", True),
        "must_have_folder": settings.get("must_have_folder_name_for_archive_to_run", False),
        "allow_overwrite": settings.get("allow_overwrite_of_spreadsheet_cells", False),
    }


def open_spreadsheet(
    settings: dict, spreadsheet_id: str = None, scopes: list[str] = READ_ONLY_SCOPES
) -> gspread.Spreadsheet:
    client = gspread.service_account(filename=settings["service_account"], scopes=scopes)
    client.set_timeout(SHEETS_TIMEOUT_SECONDS)
    if spreadsheet_id or settings["sheet_id"]:
        return client.open_by_key(spreadsheet_id or settings["sheet_id"])
    return client.open(settings["sheet"])


def allowed_tabs(spreadsheet, settings: dict) -> list:
    """The feeder's allow and block rules"""
    allow, block = settings["allow_worksheets"], settings["block_worksheets"]
    return [
        tab
        for tab in spreadsheet.worksheets()
        if (not allow or tab.title in allow) and tab.title not in block and tab.row_count >= settings["header"]
    ]


def _values(value_range: dict) -> list[list[str]]:
    return value_range.get("values", [])


def read_tabs(spreadsheet, tabs: list, settings: dict) -> dict[int, list[Row]]:
    """
    Reads only the columns sync needs, in two batched calls however many tabs there are: the headers, then the columns.
    Returns the rows by tab gid. A tab without the url and status columns is left out, as in the feeder
    """
    if not tabs:
        return {}
    header, columns = settings["header"], settings["columns"]
    headers = spreadsheet.values_batch_get(
        [gspread_utils.absolute_range_name(tab.title, f"{header}:{header}") for tab in tabs]
    )["valueRanges"]

    ranges, wanted = [], []
    for tab, value_range in zip(tabs, headers):
        names = [str(name).lower() for name in (_values(value_range) or [[]])[0]]
        positions = {
            col: names.index(columns[col].lower()) + 1
            for col in ("url", "status", *OPTIONAL_COLUMNS)
            if col in columns and columns[col].lower() in names
        }
        if "url" not in positions or "status" not in positions:
            # none of them is just a helper tab, but only one probably means a renamed or deleted header
            if ("url" in positions) != ("status" in positions):
                logger.warning(
                    f"Skipped {db.describe_tab(spreadsheet.title, tab.title)} as it is missing its url or status column"
                )
            else:
                logger.info(
                    f"Skipped {db.describe_tab(spreadsheet.title, tab.title)} as it has no url or status column"
                )
            continue
        found = [f"'{columns[col]}'" for col in positions]
        missing = [f"'{columns[col]}'" for col in OPTIONAL_COLUMNS if col in columns and col not in positions]
        # with the spreadsheet, as tab ids repeat across spreadsheets (the first tab is always 0)
        log_on_change(
            (spreadsheet.id, tab.id, "columns"),
            f"{db.describe_tab(spreadsheet.title, tab.title)}: reading the {', '.join(found)} columns"
            + (f", there's no {', '.join(missing)} column" if missing else ""),
        )
        wanted.append((tab, positions))
        for position in positions.values():
            letter = gspread_utils.rowcol_to_a1(1, position)[:-1]
            # from the header row, as a range starting below the last row of a tab is an error
            ranges.append(gspread_utils.absolute_range_name(tab.title, f"{letter}{header}:{letter}"))
    if not ranges:
        return {}

    results = iter(spreadsheet.values_batch_get(ranges)["valueRanges"])
    rows_by_tab = {}
    for tab, positions in wanted:
        cells = {}
        for col in positions:
            # a blank row inside the range is an empty list, and blanks at the end are missing
            cells[col] = [row[0] if row else "" for row in _values(next(results))][1:]

        def cell(col, index):
            values = cells.get(col, [])
            return values[index] if index < len(values) else ""

        rows_by_tab[tab.id] = []
        for index in range(len(cells["url"])):
            if url := cell("url", index).strip():
                rows_by_tab[tab.id].append(Row(header + 1 + index, url, cell("status", index), cell("folder", index)))
    return rows_by_tab


def register(conn, config_path: str, opener=open_spreadsheet) -> int:
    """Adds a config's tabs, all off, so sync starts reading them. Returns how many"""
    settings = load_settings(config_path)
    spreadsheet = opener(settings)
    tabs = allowed_tabs(spreadsheet, settings)
    for tab in tabs:
        db.save_tab(
            conn, spreadsheet.id, tab.id, tab.title, config_path, update_config=True, spreadsheet_name=spreadsheet.title
        )
    return len(tabs)


def live_tab(
    conn, sheet_id: int, rows: list[Row], snapshot_at, spreadsheet_name: str, tab_title: str, settings: dict
) -> None:
    """Applies the rules for a tab the queue owns. Only adds jobs and sheet writes: the writer updates the sheet"""
    states = {
        state["row_number"]: RowState(
            state["job_id"],
            state["job_status"],
            state["job_url"],
            state["has_unwritten"],
            state["written_since_snapshot"],
        )
        for state in db.get_row_states(conn, sheet_id, snapshot_at)
    }
    for action in decide(
        rows, states, spreadsheet_name, tab_title, settings["use_sheet_names"], settings["must_have_folder"]
    ):
        if action.kind == ADD:
            db.add_job(conn, sheet_id, action.row, action.url, action.domain, action.folder)
        elif action.kind == REJECT:
            db.add_rejected_job(conn, sheet_id, action.row, action.url, action.message)
        elif action.kind == CANCEL:
            db.cancel_job(conn, action.job_id)
        elif action.kind == ALERT:
            db.add_alert_once(conn, "sheet_disagrees", f"Sheet {sheet_id} row {action.row}: {action.message}")


def sync_cycle(conn, opener=open_spreadsheet) -> None:
    """One pass over every spreadsheet. A failure only loses that spreadsheet's turn"""
    spreadsheets = {(sheet["spreadsheet_id"], sheet["config"]) for sheet in db.get_sheets(conn)}
    for spreadsheet_id, config_path in sorted(spreadsheets):
        try:
            settings = load_settings(config_path)
            # before reading the sheet, for sync rule 1
            snapshot_at = db.now(conn)
            spreadsheet = opener(settings, spreadsheet_id)
            tabs = allowed_tabs(spreadsheet, settings)
            for tab in tabs:
                # new tabs, and renames of tabs and the spreadsheet
                db.save_tab(conn, spreadsheet_id, tab.id, tab.title, config_path, spreadsheet_name=spreadsheet.title)

            allowed_ids = {tab.id for tab in tabs}
            for sheet in db.get_sheets(conn):
                if (
                    sheet["spreadsheet_id"] == spreadsheet_id
                    and sheet["config"] == config_path
                    and sheet["worksheet_gid"] not in allowed_ids
                ):
                    tab_name = f"{db.describe_tab(spreadsheet.title, sheet['worksheet'])} (sheet {sheet['id']})"
                    # eg a tab taken out of allow_worksheets. One with jobs is kept, for its history
                    if db.remove_unused_tab(conn, sheet["id"]):
                        logger.info(f"Removed {tab_name}, as {config_path} doesn't allow it and it never had a job")
                    else:
                        logger.warning(
                            f"{tab_name} is not being read: it was deleted, or renamed or blocked so that "
                            f"{config_path} doesn't allow it"
                        )

            sheets = {
                sheet["worksheet_gid"]: sheet
                for sheet in db.get_sheets(conn)
                if sheet["spreadsheet_id"] == spreadsheet_id and sheet["config"] == config_path
            }
            # a tab that's off isn't read, as cron still owns it
            tabs = [tab for tab in tabs if tab.id in sheets and sheets[tab.id]["enabled"]]
            for gid, rows in read_tabs(spreadsheet, tabs, settings).items():
                sheet = sheets[gid]
                title, name = sheet["worksheet"], settings["sheet"] or spreadsheet.title
                blank = sum(row.status in ("", None) for row in rows)
                live_tab(conn, sheet["id"], rows, snapshot_at, name, title, settings)
                db.save_tab_read(conn, sheet["id"], len(rows), blank)
                log_on_change(
                    (spreadsheet_id, gid),
                    f"{db.describe_tab(spreadsheet.title, title)} (live): {len(rows)} row(s) with a URL, "
                    f"{blank} blank status",
                )
        except Exception as e:
            logger.error(f"Sync of config {config_path} failed: {e}")
    db.beat(conn, "sync")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sheet sync for the job queue")
    commands = parser.add_subparsers(dest="command", required=True)
    register_parser = commands.add_parser("register", help="add a config's tabs, all off")
    register_parser.add_argument("configs", nargs="+")
    run_parser = commands.add_parser("run", help="sync every registered tab that's on")
    run_parser.add_argument("--once", action="store_true", help="one pass, then exit")
    for command, help_text in (
        ("enable", "move all of a spreadsheet's tabs to the queue. Turn its cron line off first"),
        ("rollback", "take a spreadsheet off the queue, and say when its cron line can go back on"),
    ):
        commands.add_parser(command, help=help_text).add_argument("spreadsheet_id")
    args = parser.parse_args()

    if args.command == "enable":
        with db.connect() as conn:
            logger.info(f"{db.enable_spreadsheet(conn, args.spreadsheet_id)} tab(s) are now on the queue")
        return

    if args.command == "rollback":
        with db.connect() as conn:
            cancelled, running, unwritten = db.rollback_spreadsheet(conn, args.spreadsheet_id)
        logger.info(f"Off the queue. Cancelled {cancelled}, still running {running}, sheet writes to do {unwritten}")
        if running or unwritten:
            logger.warning("Keep cron off and the coordinator running, then run this again")
        else:
            logger.success("Cron can go back on")
        return

    if args.command == "register":
        with db.connect() as conn:
            for config_path in args.configs:
                logger.info(f"Registered {register(conn, config_path)} tab(s) from {config_path}")
        return

    while True:
        # a new connection each time, so a Postgres restart only loses one pass
        try:
            with db.connect() as conn:
                sync_cycle(conn)
        except Exception as e:
            logger.error(f"Sync failed: {e}")
        if args.once:
            return
        time.sleep(SYNC_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
