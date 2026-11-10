"""How sync reads sheets, against real Postgres and a fake spreadsheet that answers range reads like the Sheets API"""

import pytest
from job_queue_fakes import FakeSpreadsheet, FakeTab, opener_for

from auto_archiver.job_queue import db, sync

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
    sync.register(conn, CONFIG, opener_for(sheet))  # off, eg after a rollback
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


def test_google_connection_is_read_only(mocker):
    connect = mocker.patch("auto_archiver.job_queue.sync.gspread.service_account")
    sync.open_spreadsheet({"service_account": "account.json", "sheet_id": "spreadsheet1", "sheet": None})
    scopes = connect.call_args.kwargs["scopes"]
    assert scopes and all(scope.endswith(".readonly") for scope in scopes)


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
