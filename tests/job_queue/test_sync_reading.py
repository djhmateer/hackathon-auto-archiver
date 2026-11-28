"""How sync reads sheets, against real Postgres and a fake spreadsheet that answers range reads like the Sheets API"""

from unittest.mock import Mock

import gspread
import pytest
from job_queue_fakes import FakeSpreadsheet, FakeTab, opener_for

from auto_archiver.job_queue import db, sync, writer

HEADERS = ["Link", "Archive status", "Destination folder", "Text content"]
CONFIG = "orchestration-example.yaml"


def tab_of(*rows, gid=1, title="Tab 1", headers=HEADERS):
    return FakeTab(gid, title, [headers, *rows])


def row(url, status="", folder=""):
    return [url, status, folder, "x" * 50]


@pytest.fixture
def config(tmp_path, monkeypatch):
    def write(**settings):
        lines = "".join(f"  {key}: {value}\n" for key, value in settings.items())
        (tmp_path / CONFIG).write_text(f"gsheet_feeder_db:\n  sheet_id: spreadsheet1\n{lines}")

    monkeypatch.chdir(tmp_path)
    write()
    return write


def queued(conn):
    return conn.execute("SELECT * FROM jobs WHERE status = 'queued' ORDER BY id").fetchall()


def run(conn, spreadsheet):
    sync.sync_cycle(conn, opener_for(spreadsheet))


def setup_sheet(conn, spreadsheet):
    """Registers the tabs and turns them on, as a tab that's off isn't read"""
    sync.register(conn, CONFIG, opener_for(spreadsheet))
    conn.execute("UPDATE sheets SET enabled = true")


def test_register_adds_tabs_switched_off(conn, config):
    sheet = FakeSpreadsheet([tab_of(gid=1), tab_of(gid=2, title="Tab 2")])
    assert sync.register(conn, CONFIG, opener_for(sheet)) == 2
    sheets = db.get_sheets(conn)
    assert [(s["worksheet_gid"], s["worksheet"], s["enabled"], s["config"]) for s in sheets] == [
        (1, "Tab 1", False, CONFIG),
        (2, "Tab 2", False, CONFIG),
    ]


def test_add_registers_a_new_config_and_turns_it_on(conn, config):
    sheet = FakeSpreadsheet([tab_of(gid=1), tab_of(gid=2, title="Tab 2")])
    assert sync.add(conn, CONFIG, opener_for(sheet)) == 2
    assert [(s["worksheet"], s["enabled"]) for s in db.get_sheets(conn)] == [("Tab 1", True), ("Tab 2", True)]


def test_add_leaves_a_config_already_on_the_queue_as_it_is(conn, config):
    sheet = FakeSpreadsheet([tab_of()])
    sync.register(conn, CONFIG, opener_for(sheet))  # off, eg after `sync disable`
    assert sync.add(conn, CONFIG, opener_for(sheet)) is None
    assert [s["enabled"] for s in db.get_sheets(conn)] == [False]


def test_register_twice_changes_nothing(conn, config):
    sheet = FakeSpreadsheet([tab_of()])
    setup_sheet(conn, sheet)
    setup_sheet(conn, sheet)
    assert len(db.get_sheets(conn)) == 1


def test_register_keeps_to_the_allow_and_block_lists(conn, config):
    config(block_worksheets="['Tab 2']")
    sheet = FakeSpreadsheet([tab_of(gid=1), tab_of(gid=2, title="Tab 2")])
    assert sync.register(conn, CONFIG, opener_for(sheet)) == 1
    config(allow_worksheets="['Tab 2']")
    assert sync.register(conn, CONFIG, opener_for(sheet)) == 1


def test_a_tab_that_is_off_is_not_read(conn, config):
    sheet = FakeSpreadsheet([tab_of(row("https://example.com/1"))])
    sync.register(conn, CONFIG, opener_for(sheet))
    run(conn, sheet)
    assert sheet.ranges_read == []
    assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 0


def test_reads_only_the_needed_columns_in_two_calls(conn, config):
    sheet = FakeSpreadsheet([tab_of(row("https://example.com/1")), tab_of(gid=2, title="Tab 2")])
    setup_sheet(conn, sheet)
    run(conn, sheet)
    assert sheet.batch_calls == 2
    assert sheet.ranges_read == [
        "'Tab 1'!1:1",
        "'Tab 2'!1:1",
        "'Tab 1'!A1:A",
        "'Tab 1'!B1:B",
        "'Tab 1'!C1:C",
        "'Tab 2'!A1:A",
        "'Tab 2'!B1:B",
        "'Tab 2'!C1:C",
    ]


def test_blank_rows_inside_a_column_keep_row_numbers_right(conn, config):
    sheet = FakeSpreadsheet(
        [tab_of(row(""), row("https://example.com/3"), row("https://example.com/4", "success"), row(""))]
    )
    setup_sheet(conn, sheet)
    run(conn, sheet)
    assert [j["row_number"] for j in queued(conn)] == [3]


def test_custom_header_row_and_column_names(conn, config):
    config(header=2, columns='{"url": "the url", "status": "the status"}')
    tab = FakeTab(1, "Tab 1", [["a title row"], ["The URL", "x", "The Status"], ["https://example.com/1", "", ""]])
    sheet = FakeSpreadsheet([tab])
    setup_sheet(conn, sheet)
    run(conn, sheet)
    assert sheet.ranges_read[1:] == ["'Tab 1'!A2:A", "'Tab 1'!C2:C"]
    assert [j["row_number"] for j in queued(conn)] == [3]


def test_tab_title_needing_quotes_is_read(conn, config):
    sheet = FakeSpreadsheet([tab_of(row("https://example.com/1"), title="Dave's tab")])
    setup_sheet(conn, sheet)
    run(conn, sheet)
    assert len(queued(conn)) == 1


def test_helper_tab_without_the_columns_is_skipped(conn, config):
    helper = tab_of(["some", "notes"], gid=2, title="Notes", headers=["Some", "Headers"])
    sheet = FakeSpreadsheet([tab_of(row("https://example.com/1")), helper])
    setup_sheet(conn, sheet)
    run(conn, sheet)
    assert len(queued(conn)) == 1


def test_new_tab_is_found_and_a_rename_is_kept(conn, config):
    first = tab_of(row("https://example.com/1"))
    sheet = FakeSpreadsheet([first])
    setup_sheet(conn, sheet)
    run(conn, sheet)
    first.title = "Renamed"
    sheet.tabs.append(tab_of(row("https://example.com/2"), gid=2, title="New tab"))
    run(conn, sheet)
    assert [s["worksheet"] for s in db.get_sheets(conn)] == ["Renamed", "New tab"]
    assert len(queued(conn)) == 2, "the renamed tab's job isn't duplicated"


def test_spreadsheet_name_is_saved_and_kept_up_to_date(conn, config):
    sheet = FakeSpreadsheet([tab_of(row("https://example.com/1"))])
    setup_sheet(conn, sheet)
    assert db.get_sheets(conn)[0]["spreadsheet_name"] == "Example Sheet"
    sheet.title = "Renamed Sheet"
    run(conn, sheet)
    assert db.get_sheets(conn)[0]["spreadsheet_name"] == "Renamed Sheet"
    db.save_tab(conn, "spreadsheet1", 1, "Tab 1", CONFIG)
    assert db.get_sheets(conn)[0]["spreadsheet_name"] == "Renamed Sheet", "no name given keeps the one there is"


def test_row_without_a_folder_is_not_recorded_when_one_is_required(conn, config):
    config(must_have_folder_name_for_archive_to_run="true")
    sheet = FakeSpreadsheet(
        [
            tab_of(
                row("https://example.com/2", folder="a folder"),
                row("https://example.com/3"),
                row("https://example.com/4", folder="   "),
            )
        ]
    )
    setup_sheet(conn, sheet)
    run(conn, sheet)
    assert [j["row_number"] for j in queued(conn)] == [2]


def test_tab_taken_out_of_the_allow_list_with_no_jobs_is_removed(conn, config, mocker):
    sheet = FakeSpreadsheet([tab_of(gid=1), tab_of(gid=2, title="Tab 2")])
    setup_sheet(conn, sheet)

    logger = mocker.patch("auto_archiver.job_queue.sync.logger")
    config(allow_worksheets="['Tab 1']")
    run(conn, sheet)

    assert [s["worksheet"] for s in db.get_sheets(conn)] == ["Tab 1"]
    removed = logger.info.call_args_list[0].args[0]
    assert "tab Tab 2 (sheet 2), as orchestration-example.yaml doesn't allow it" in removed
    logger.warning.assert_not_called()


def test_tab_renamed_out_of_the_allow_list_gets_a_warning(conn, config, mocker):
    config(allow_worksheets="['Tab 1']")
    tab = tab_of(row("https://example.com/1"))
    sheet = FakeSpreadsheet([tab])
    setup_sheet(conn, sheet)
    run(conn, sheet)

    logger = mocker.patch("auto_archiver.job_queue.sync.logger")
    tab.title = "Renamed"
    run(conn, sheet)

    assert "Tab 1 (sheet 1) is not being read" in logger.warning.call_args.args[0]
    assert len(queued(conn)) == 1, "its job is left as it was"


def test_one_broken_spreadsheet_does_not_stop_the_others(conn, config):
    good = FakeSpreadsheet([tab_of(row("https://example.com/1"))], spreadsheet_id="good")
    db.save_tab(conn, "bad", 1, "Tab 1", CONFIG)
    db.save_tab(conn, "good", 1, "Tab 1", CONFIG)
    conn.execute("UPDATE sheets SET enabled = true")

    def opener(settings, spreadsheet_id=None):
        if spreadsheet_id == "bad":
            raise RuntimeError("a Sheets outage")
        return good

    sync.sync_cycle(conn, opener)
    assert len(queued(conn)) == 1
    assert conn.execute("SELECT count(*) AS n FROM heartbeats WHERE name = 'sync'").fetchone()["n"] == 1


def test_spreadsheets_share_a_client_per_service_account_and_scopes(mocker):
    # a new client was a token fetch on every open
    connect = mocker.patch("auto_archiver.job_queue.sync.gspread.service_account")
    settings = {"service_account": "account.json", "sheet_id": "spreadsheet1", "sheet": None}
    sync.open_spreadsheet(settings)
    sync.open_spreadsheet(settings, "spreadsheet2")
    assert connect.call_count == 1
    writer.open_for_writing(settings)
    sync.open_spreadsheet({**settings, "service_account": "other.json"})
    assert connect.call_count == 3, "the writer's scopes and another account each get their own"


def test_google_connection_is_read_only(mocker):
    connect = mocker.patch("auto_archiver.job_queue.sync.gspread.service_account")
    sync.open_spreadsheet({"service_account": "account.json", "sheet_id": "spreadsheet1", "sheet": None})
    scopes = connect.call_args.kwargs["scopes"]
    assert scopes and all(scope.endswith(".readonly") for scope in scopes)


def test_opening_a_spreadsheet_fetches_only_the_metadata_sync_needs(mocker):
    # the full metadata (each tab's merges, formats etc) was 42 MB for one spreadsheet
    class FakeHTTPClient(gspread.http_client.HTTPClient):
        def __init__(self):
            self.params = []

        def fetch_sheet_metadata(self, id, params=None):
            self.params.append(params)
            tab = {"sheetId": 7, "title": "Tab 1", "index": 0, "gridProperties": {"rowCount": 10, "columnCount": 5}}
            return {"properties": {"title": "Example Sheet"}, "sheets": [{"properties": tab}]}

    http_client = FakeHTTPClient()
    mocker.patch("auto_archiver.job_queue.sync.gspread.service_account").return_value.http_client = http_client

    spreadsheet = sync.open_spreadsheet({"service_account": "account.json", "sheet_id": "spreadsheet1", "sheet": None})
    (tab,) = spreadsheet.worksheets()

    assert http_client.params == [sync.METADATA_PARAMS], "once: worksheets() straight after reuses the opening's"
    assert (spreadsheet.id, spreadsheet.title) == ("spreadsheet1", "Example Sheet")
    assert (tab.id, tab.title, tab.row_count) == (7, "Tab 1", 10)

    spreadsheet.worksheets()
    assert http_client.params == [sync.METADATA_PARAMS] * 2, "a later worksheets() fetches the tabs again"

    spreadsheet.fetch_sheet_metadata(params={"fields": "namedRanges"})  # eg gspread's list_named_ranges()
    assert http_client.params[-1] == {"fields": "namedRanges"}, "a call asking for particular fields still gets them"


def test_columns_found_are_logged_once(conn, config, caplog):
    sheet = FakeSpreadsheet([tab_of(row("https://example.com/1"))])
    setup_sheet(conn, sheet)
    run(conn, sheet)
    run(conn, sheet)
    line = "Sheet 'Example Sheet' tab Tab 1: reading the 'link', 'archive status', 'destination folder' columns"
    assert caplog.text.count(line) == 1


def test_tab_summary_is_only_logged_when_it_changes(conn, config, caplog):
    tab = tab_of(row("https://example.com/1"))
    sheet = FakeSpreadsheet([tab])
    setup_sheet(conn, sheet)
    run(conn, sheet)
    run(conn, sheet)
    assert caplog.text.count("row(s) with a URL") == 1
    tab.rows.append(row("https://example.com/2"))
    run(conn, sheet)
    assert "Sheet 'Example Sheet' tab Tab 1 (live): 2 row(s) with a URL" in caplog.text


def test_tabs_with_the_same_id_in_different_spreadsheets_are_each_logged_once(conn, config, caplog):
    # the first tab of every spreadsheet has id 0, so the logging has to tell them apart by spreadsheet too
    first = FakeSpreadsheet([tab_of(row("https://example.com/1"), gid=0)], spreadsheet_id="first", title="First")
    second = FakeSpreadsheet([tab_of(row("https://example.com/2"), gid=0)], spreadsheet_id="second", title="Second")
    for sheet in (first, second):
        setup_sheet(conn, sheet)
    spreadsheets = {"first": first, "second": second}

    def opener(settings, spreadsheet_id=None):
        return spreadsheets[spreadsheet_id]

    sync.sync_cycle(conn, opener)
    sync.sync_cycle(conn, opener)
    assert caplog.text.count("Sheet 'First' tab Tab 1 (live)") == 1
    assert caplog.text.count("Sheet 'Second' tab Tab 1 (live)") == 1
    assert caplog.text.count("reading the") == 2


def test_saves_what_it_read_for_the_dashboard(conn, config):
    sheet = FakeSpreadsheet([tab_of(row("https://example.com/1"), row("https://example.com/2", status="done"))])
    setup_sheet(conn, sheet)
    run(conn, sheet)
    tab = conn.execute("SELECT url_rows, blank_rows, last_read_at FROM sheets").fetchone()
    assert (tab["url_rows"], tab["blank_rows"]) == (2, 1)
    assert tab["last_read_at"] is not None


def test_a_tab_missing_its_status_column_has_an_error_until_its_fixed(conn, config):
    tab = tab_of(["https://example.com/1"], headers=["Link"])
    sheet = FakeSpreadsheet([tab])
    setup_sheet(conn, sheet)
    run(conn, sheet)
    saved = conn.execute("SELECT read_error, last_read_at FROM sheets").fetchone()
    assert saved["read_error"] == "no 'archive status' column in row 1, so it isn't read"
    assert saved["last_read_at"] is None

    tab.rows[0] = HEADERS
    sync._last_read.clear()
    run(conn, sheet)
    assert conn.execute("SELECT read_error FROM sheets").fetchone()["read_error"] is None


def test_a_slow_spreadsheet_is_logged_with_where_the_time_went(conn, config, caplog, monkeypatch):
    sheet = FakeSpreadsheet([tab_of(row("https://example.com/1"))])
    setup_sheet(conn, sheet)
    monkeypatch.setattr(sync, "SLOW_SPREADSHEET_SECONDS", 5)
    run(conn, sheet)
    assert "Sync of sheet" not in caplog.text  # quick, so not logged

    monkeypatch.setattr(sync, "SLOW_SPREADSHEET_SECONDS", 0)
    run(conn, sheet)
    assert "Sync of sheet 'Example Sheet' took " in caplog.text
    assert "reading 1 tab(s) " in caplog.text


def test_after_each_runs_after_every_spreadsheet_even_a_broken_one(conn, config):
    good = FakeSpreadsheet([tab_of(row("https://example.com/1"))], spreadsheet_id="good")
    db.save_tab(conn, "bad", 1, "Tab 1", CONFIG)
    db.save_tab(conn, "good", 1, "Tab 1", CONFIG)
    conn.execute("UPDATE sheets SET enabled = true")

    def opener(settings, spreadsheet_id=None):
        if spreadsheet_id == "bad":
            raise RuntimeError("a Sheets outage")
        return good

    calls = []
    sync.sync_cycle(conn, opener, after_each=lambda: calls.append(1))
    assert len(calls) == 2


@pytest.fixture
def quiet_tabs_every_5_minutes(monkeypatch):
    monkeypatch.setattr(sync, "QUIET_TAB_SECONDS", 5 * 60)


def test_quiet_tab_is_read_every_5_minutes_not_every_pass(conn, config, quiet_tabs_every_5_minutes):
    sheet = FakeSpreadsheet([tab_of(row("https://example.com/1", status="done"))])
    setup_sheet(conn, sheet)
    opened = []

    def opener(settings, spreadsheet_id=None):
        opened.append(spreadsheet_id)
        return sheet

    sync.sync_cycle(conn, opener)
    sync.sync_cycle(conn, opener)
    assert len(opened) == 1

    for sheet_id in sync._last_read:
        sync._last_read[sheet_id] -= 5 * 60
    sync.sync_cycle(conn, opener)
    assert len(opened) == 2


def test_tab_with_a_job_this_week_is_read_every_pass(conn, config, quiet_tabs_every_5_minutes):
    sheet = FakeSpreadsheet([tab_of(row("https://example.com/1"))])
    setup_sheet(conn, sheet)
    run(conn, sheet)
    assert len(queued(conn)) == 1
    calls = sheet.batch_calls

    run(conn, sheet)
    assert sheet.batch_calls == calls * 2


def test_tab_whose_last_job_is_older_than_a_week_is_quiet(conn, config, quiet_tabs_every_5_minutes):
    sheet = FakeSpreadsheet([tab_of(row("https://example.com/1"))])
    setup_sheet(conn, sheet)
    run(conn, sheet)
    conn.execute("UPDATE jobs SET added_at = now() - interval '8 days'")
    calls = sheet.batch_calls

    run(conn, sheet)
    assert sheet.batch_calls == calls


def test_only_the_due_tabs_of_a_spreadsheet_are_read(conn, config, quiet_tabs_every_5_minutes):
    sheet = FakeSpreadsheet(
        [
            tab_of(row("https://example.com/1"), gid=1),
            tab_of(row("https://example.com/2", status="done"), gid=2, title="Tab 2"),
        ]
    )
    setup_sheet(conn, sheet)
    run(conn, sheet)
    sheet.ranges_read.clear()

    run(conn, sheet)
    assert sheet.ranges_read and all(r.startswith("'Tab 1'!") for r in sheet.ranges_read)


def test_quiet_tab_whose_read_failed_is_read_again_next_pass(conn, config, quiet_tabs_every_5_minutes, mocker):
    sheet = FakeSpreadsheet([tab_of(row("https://example.com/1", status="done"))])
    setup_sheet(conn, sheet)
    mocker.patch.object(sync, "read_tabs", side_effect=RuntimeError("Sheets timeout"))
    run(conn, sheet)
    mocker.stopall()

    run(conn, sheet)
    assert sheet.batch_calls == 2


class FakeSession:
    """Answers a Sheets API call after `seconds` on a fake clock, or raises `error`"""

    def __init__(self, clock, seconds=0.0, error=None):
        self.clock, self.seconds, self.error = clock, seconds, error

    def request(self, **kwargs):
        self.clock[0] += self.seconds
        if self.error:
            raise self.error
        return Mock(ok=True, content=b"x" * 2048)


@pytest.fixture
def clock(mocker):
    now = [100.0]
    mocker.patch("auto_archiver.job_queue.sync.time.monotonic", side_effect=lambda: now[0])
    return now


ENDPOINT = "https://sheets.example.com/v4/spreadsheets/spreadsheet1/values:batchGet"


def test_a_slow_sheets_call_is_logged_with_how_long_it_took(mocker, clock):
    logger = mocker.patch("auto_archiver.job_queue.sync.logger")
    sync.TimedHTTPClient(None, session=FakeSession(clock, seconds=sync.SLOW_REQUEST_SECONDS - 1)).request(
        "get", ENDPOINT
    )
    logger.info.assert_not_called()
    sync.TimedHTTPClient(None, session=FakeSession(clock, seconds=12.5)).request("get", ENDPOINT)
    logger.info.assert_called_once_with("Google API GET /v4/spreadsheets/spreadsheet1/values:batchGet took 12.5s, 2 KB")


def test_a_failed_sheets_call_is_logged_with_how_long_it_took(mocker, clock):
    logger = mocker.patch("auto_archiver.job_queue.sync.logger")
    client = sync.TimedHTTPClient(None, session=FakeSession(clock, seconds=60, error=TimeoutError("read timed out")))
    with pytest.raises(TimeoutError):
        client.request("get", ENDPOINT)
    logger.info.assert_called_once_with(
        "Google API GET /v4/spreadsheets/spreadsheet1/values:batchGet failed after 60.0s: TimeoutError"
    )


def test_sync_and_the_writer_time_their_sheets_calls(mocker):
    connect = mocker.patch("auto_archiver.job_queue.sync.gspread.service_account")
    sync.open_spreadsheet({"service_account": "account.json", "sheet_id": "spreadsheet1", "sheet": None})
    assert connect.call_args.kwargs["http_client"] is sync.TimedHTTPClient
