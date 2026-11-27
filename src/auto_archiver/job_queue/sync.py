"""
Sheet sync: reads the sheets and works out which rows need a job. See queue-spec.md

decide() holds the sync rules with no network or database. They run on each tab with sheets.enabled on, adding jobs
and sheet writes. A tab that's off isn't read, eg a test spreadsheet turned off with `sync disable`.

Sync only ever connects to Google read-only. The sheet writer is the only part that writes to a sheet.

    python -m auto_archiver.job_queue.sync register secrets/<config>.yaml
    python -m auto_archiver.job_queue.sync run [--once]
"""

import argparse
import json
import os
import time
from dataclasses import dataclass
from urllib.parse import urlparse

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
# a Google API call (Sheets, or Drive to find a spreadsheet by name) that takes longer, or fails, is logged with how long it took (see TimedHTTPClient)
SLOW_REQUEST_SECONDS = 10
# all sync and the writer need from a spreadsheet's metadata (see SlimSpreadsheet)
METADATA_PARAMS = {"includeGridData": "false", "fields": "properties,sheets.properties"}
SYNC_INTERVAL_SECONDS = 60
# a spreadsheet whose sync takes longer is logged with where the time went
# DM 27th Nov 26 - 0 for a day, to see where every spreadsheet's time goes before cutting the calls. Then back to 5
SLOW_SPREADSHEET_SECONDS = 0
# DM 20th Nov 26 - a pass over every tab took ~50s, so new rows waited up to a minute. Now a tab with a job created in
# the last ACTIVE_DAYS is read every pass (the coordinator's SYNC_SECONDS, 15s), and the rest every QUIET_TAB_SECONDS
ACTIVE_DAYS = 7
QUIET_TAB_SECONDS = 5 * 60
# when sync last read each tab, by sheets.id, in time.monotonic() seconds
_last_read = {}
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


class TimedHTTPClient(gspread.http_client.HTTPClient):
    """
    gspread's, but logs each Google API call that takes over SLOW_REQUEST_SECONDS or fails, with how long it took.
    DM 24th Nov 26 - a call that times out holds up the whole sync loop for SHEETS_TIMEOUT_SECONDS. This shows how long
    the calls that succeed take, to see whether a shorter timeout would cut them off
    """

    def request(self, method, endpoint, *args, **kwargs):
        started = time.monotonic()
        # the path, eg /v4/spreadsheets/<id>/values:batchGet, without the host
        call = f"{method.upper()} {urlparse(endpoint).path}"
        try:
            response = super().request(method, endpoint, *args, **kwargs)
        except Exception as e:
            logger.info(f"Google API {call} failed after {time.monotonic() - started:.1f}s: {type(e).__name__}")
            raise
        took = time.monotonic() - started
        if took >= SLOW_REQUEST_SECONDS:
            logger.info(f"Google API {call} took {took:.1f}s, {len(response.content) // 1024} KB")
        return response


class SlimSpreadsheet(gspread.Spreadsheet):
    """
    gspread's, but its metadata calls that ask for no particular fields (on opening, worksheets(), worksheet() etc.)
    fetch only the spreadsheet's and its tabs' properties: title, ids, row counts, which is all those use. The full
    metadata adds every tab's merges, conditional formats etc.
    DM 24th Nov 26 - that was 42 MB for one spreadsheet, taking ~4s, twice per sync of it and per write batch
    """

    def fetch_sheet_metadata(self, params=None):
        return super().fetch_sheet_metadata(params or METADATA_PARAMS)


def open_spreadsheet(
    settings: dict, spreadsheet_id: str = None, scopes: list[str] = READ_ONLY_SCOPES
) -> gspread.Spreadsheet:
    client = gspread.service_account(filename=settings["service_account"], scopes=scopes, http_client=TimedHTTPClient)
    client.set_timeout(SHEETS_TIMEOUT_SECONDS)
    if spreadsheet_id or settings["sheet_id"]:
        # as client.open_by_key() does, but with the smaller metadata
        return SlimSpreadsheet(client.http_client, {"id": spreadsheet_id or settings["sheet_id"]})
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


def read_tabs(spreadsheet, tabs: list, settings: dict) -> tuple[dict[int, list[Row]], dict[int, str]]:
    """
    Reads only the columns sync needs, in two batched calls however many tabs there are: the headers, then the columns.
    Returns the rows by tab gid, and why each tab left out was, by gid. A tab without the url and status columns is
    left out, as in the feeder
    """
    errors = {}
    if not tabs:
        return {}, errors
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
            absent = [f"'{columns[col]}'" for col in ("url", "status") if col not in positions]
            errors[tab.id] = f"no {' or '.join(absent)} column in row {header}, so it isn't read"
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
        return {}, errors

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
    return rows_by_tab, errors


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


def add(conn, config_path: str, opener=open_spreadsheet) -> int | None:
    """
    For a deploy: registers a config that isn't on the queue yet and turns its tabs on. Returns how many tabs, or None
    if it was already there. One that is, eg turned off by `sync disable`, is left as it is
    """
    if any(sheet["config"] == config_path for sheet in db.get_sheets(conn)):
        return None
    register(conn, config_path, opener)
    spreadsheet_ids = {sheet["spreadsheet_id"] for sheet in db.get_sheets(conn) if sheet["config"] == config_path}
    return sum(db.enable_spreadsheet(conn, spreadsheet_id) for spreadsheet_id in spreadsheet_ids)


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


def is_due(sheet: dict, active: set[int]) -> bool:
    """
    Whether to read the tab this pass: it's on (sheets.enabled), and it's active or
    hasn't been read for QUIET_TAB_SECONDS, or ever
    """
    last_read = _last_read.get(sheet["id"], float("-inf"))
    return sheet["enabled"] and (sheet["id"] in active or time.monotonic() - last_read >= QUIET_TAB_SECONDS)


def sync_cycle(conn, opener=open_spreadsheet, after_each=None) -> None:
    """
    One pass over the spreadsheets with a tab that's due (see is_due), reading only those tabs. A failure only
    loses that spreadsheet's turn. after_each runs after each spreadsheet, eg the sheet writer, so a long pass doesn't
    hold it up
    """
    active = db.get_recently_active_sheet_ids(conn, ACTIVE_DAYS)
    spreadsheets = {
        (sheet["spreadsheet_id"], sheet["config"]) for sheet in db.get_sheets(conn) if is_due(sheet, active)
    }
    for spreadsheet_id, config_path in sorted(spreadsheets):
        try:
            started = time.monotonic()
            settings = load_settings(config_path)
            # before reading the sheet, for sync rule 1
            snapshot_at = db.now(conn)
            spreadsheet = opener(settings, spreadsheet_id)
            # opening is the token fetch and a metadata call, listing the tabs another metadata call
            opened = time.monotonic()
            tabs = allowed_tabs(spreadsheet, settings)
            listed = time.monotonic()
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
            # a tab new this pass has never been read, so is due and its rows are queued straight away
            tabs = [tab for tab in tabs if tab.id in sheets and is_due(sheets[tab.id], active)]
            rows_by_tab, errors = read_tabs(spreadsheet, tabs, settings)
            read = time.monotonic()
            for gid, error in errors.items():
                db.save_tab_error(conn, sheets[gid]["id"], error)
            for gid, rows in rows_by_tab.items():
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
            # only once all went well, so a failure is retried next pass
            for tab in tabs:
                _last_read[sheets[tab.id]["id"]] = time.monotonic()
            took = time.monotonic() - started
            if took >= SLOW_SPREADSHEET_SECONDS:
                logger.info(
                    f"Sync of sheet '{spreadsheet.title}' took {took:.1f}s: opening {opened - started:.1f}s, "
                    f"listing its tabs {listed - opened:.1f}s, reading {len(tabs)} tab(s) {read - listed:.1f}s, "
                    f"working out the jobs {time.monotonic() - read:.1f}s"
                )
        except Exception as e:
            logger.error(f"Sync of config {config_path} failed: {e}")
        if after_each is not None:
            after_each()
    db.beat(conn, "sync")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sheet sync for the job queue")
    commands = parser.add_subparsers(dest="command", required=True)
    register_parser = commands.add_parser("register", help="add a config's tabs, all off")
    register_parser.add_argument("configs", nargs="+")
    add_parser = commands.add_parser("add", help="register and turn on each config not on the queue yet, for a deploy")
    add_parser.add_argument("configs", nargs="+")
    run_parser = commands.add_parser("run", help="sync every registered tab that's on")
    run_parser.add_argument("--once", action="store_true", help="one pass, then exit")
    for command, help_text in (
        ("enable", "turn on all of a spreadsheet's tabs, so sync reads them"),
        ("disable", "turn off all of a spreadsheet's tabs and cancel their waiting jobs; its config stays registered"),
    ):
        commands.add_parser(command, help=help_text).add_argument("spreadsheet_id")
    args = parser.parse_args()

    if args.command == "enable":
        with db.connect() as conn:
            logger.info(f"{db.enable_spreadsheet(conn, args.spreadsheet_id)} tab(s) are now on")
        return

    if args.command == "disable":
        with db.connect() as conn:
            cancelled, running, unwritten = db.disable_spreadsheet(conn, args.spreadsheet_id)
        logger.info(f"Turned off. Cancelled {cancelled}, still running {running}, sheet writes to do {unwritten}")
        if running or unwritten:
            logger.warning("Its running jobs will still finish and be written to the sheet. Run this again to check")
        else:
            logger.success("Nothing left to finish or write")
        return

    if args.command == "add":
        with db.connect() as conn:
            for config_path in args.configs:
                tabs = add(conn, config_path)
                if tabs is None:
                    logger.info(f"{config_path} is already on the queue")
                else:
                    logger.info(f"Added {config_path}: {tabs} tab(s) registered and on the queue")
                    # a new spreadsheet has no organisation, so no pool: its jobs wait until it's set
                    logger.warning(
                        f"Set its organisation, or no worker claims its jobs. On the coordinator: UPDATE sheets "
                        f"SET organisation_id = <id from organisations> WHERE config = '{config_path}'"
                    )
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
