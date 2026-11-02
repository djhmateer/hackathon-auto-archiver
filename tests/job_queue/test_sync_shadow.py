"""Shadow mode against real Postgres and a fake spreadsheet that answers range reads like the Sheets API"""

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


def shadow_jobs(conn, open_only=True):
    where = "WHERE closed_at IS NULL" if open_only else ""
    return conn.execute(f"SELECT * FROM shadow_jobs {where} ORDER BY id").fetchall()


def run(conn, spreadsheet):
    sync.sync_cycle(conn, opener_for(spreadsheet))


def setup_sheet(conn, spreadsheet):
    sync.register(conn, CONFIG, opener_for(spreadsheet))


def test_register_adds_tabs_switched_off(conn, config):
    sheet = FakeSpreadsheet([tab_of(gid=1), tab_of(gid=2, title="Tab 2")])
    assert sync.register(conn, CONFIG, opener_for(sheet)) == 2
    sheets = db.get_sheets(conn)
    assert [(s["worksheet_gid"], s["worksheet"], s["enabled"], s["config"]) for s in sheets] == [
        (1, "Tab 1", False, CONFIG),
        (2, "Tab 2", False, CONFIG),
    ]


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


def test_blank_rows_are_recorded_and_others_are_not(conn, config):
    sheet = FakeSpreadsheet(
        [
            tab_of(
                row("https://example.com/1"),
                row("https://example.com/2", "success"),
                row(""),
                row("https://example.com/4"),
                row("N/A"),
                row("https://example.com/6", "Archive in progress"),
            )
        ]
    )
    setup_sheet(conn, sheet)
    run(conn, sheet)
    assert [(j["row_number"], j["url"]) for j in shadow_jobs(conn)] == [
        (2, "https://example.com/1"),
        (5, "https://example.com/4"),
    ]
    assert conn.execute("SELECT count(*) AS n FROM alerts").fetchone()["n"] == 0, "cron's statuses are normal"
    assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM outbox").fetchone()["n"] == 0


def test_second_pass_adds_no_duplicates_and_keeps_added_at(conn, config):
    sheet = FakeSpreadsheet([tab_of(row("https://example.com/1"))])
    setup_sheet(conn, sheet)
    run(conn, sheet)
    first = shadow_jobs(conn)
    run(conn, sheet)
    assert shadow_jobs(conn) == first
    assert sheet.tabs[0].writes == [], "shadow mode never writes to a sheet"


def test_cron_picking_up_a_row_closes_it(conn, config):
    tab = tab_of(row("https://example.com/1"))
    sheet = FakeSpreadsheet([tab])
    setup_sheet(conn, sheet)
    run(conn, sheet)
    tab.rows[1] = row("https://example.com/1", "Archive in progress")
    run(conn, sheet)
    assert shadow_jobs(conn) == []
    assert shadow_jobs(conn, open_only=False)[0]["closed_at"] is not None


def test_client_clearing_a_finished_row_records_it_again(conn, config):
    tab = tab_of(row("https://example.com/1"))
    sheet = FakeSpreadsheet([tab])
    setup_sheet(conn, sheet)
    run(conn, sheet)
    tab.rows[1] = row("https://example.com/1", "success")
    run(conn, sheet)
    tab.rows[1] = row("https://example.com/1")
    run(conn, sheet)
    assert len(shadow_jobs(conn)) == 1
    assert len(shadow_jobs(conn, open_only=False)) == 2


def test_changed_url_closes_it_then_records_the_new_one(conn, config):
    tab = tab_of(row("https://example.com/1"))
    sheet = FakeSpreadsheet([tab])
    setup_sheet(conn, sheet)
    run(conn, sheet)
    tab.rows[1] = row("https://example.com/new")
    run(conn, sheet)
    assert shadow_jobs(conn) == []
    run(conn, sheet)
    assert [j["url"] for j in shadow_jobs(conn)] == ["https://example.com/new"]


def test_url_removed_from_a_row_closes_it(conn, config):
    tab = tab_of(row("https://example.com/1"), row("https://example.com/2"))
    sheet = FakeSpreadsheet([tab])
    setup_sheet(conn, sheet)
    run(conn, sheet)
    tab.rows[1] = row("")
    run(conn, sheet)
    assert [j["row_number"] for j in shadow_jobs(conn)] == [3]


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
    assert [j["row_number"] for j in shadow_jobs(conn)] == [3]


def test_custom_header_row_and_column_names(conn, config):
    config(header=2, columns='{"url": "the url", "status": "the status"}')
    tab = FakeTab(1, "Tab 1", [["a title row"], ["The URL", "x", "The Status"], ["https://example.com/1", "", ""]])
    sheet = FakeSpreadsheet([tab])
    setup_sheet(conn, sheet)
    run(conn, sheet)
    assert sheet.ranges_read[1:] == ["'Tab 1'!A2:A", "'Tab 1'!C2:C"]
    assert [j["row_number"] for j in shadow_jobs(conn)] == [3]


def test_tab_title_needing_quotes_is_read(conn, config):
    sheet = FakeSpreadsheet([tab_of(row("https://example.com/1"), title="Dave's tab")])
    setup_sheet(conn, sheet)
    run(conn, sheet)
    assert len(shadow_jobs(conn)) == 1


def test_helper_tab_without_the_columns_is_skipped(conn, config):
    helper = tab_of(["some", "notes"], gid=2, title="Notes", headers=["Some", "Headers"])
    sheet = FakeSpreadsheet([tab_of(row("https://example.com/1")), helper])
    setup_sheet(conn, sheet)
    run(conn, sheet)
    assert len(shadow_jobs(conn)) == 1


def test_new_tab_is_found_and_a_rename_is_kept(conn, config):
    first = tab_of(row("https://example.com/1"))
    sheet = FakeSpreadsheet([first])
    setup_sheet(conn, sheet)
    run(conn, sheet)
    first.title = "Renamed"
    sheet.tabs.append(tab_of(row("https://example.com/2"), gid=2, title="New tab"))
    run(conn, sheet)
    assert [s["worksheet"] for s in db.get_sheets(conn)] == ["Renamed", "New tab"]
    assert len(shadow_jobs(conn)) == 2, "the renamed tab's job stays open, not duplicated"


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
    assert [j["row_number"] for j in shadow_jobs(conn)] == [2]


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
    assert len(shadow_jobs(conn)) == 1, "its job is left as it was"


def test_one_broken_spreadsheet_does_not_stop_the_others(conn, config):
    good = FakeSpreadsheet([tab_of(row("https://example.com/1"))], spreadsheet_id="good")
    db.save_tab(conn, "bad", 1, "Tab 1", CONFIG)
    db.save_tab(conn, "good", 1, "Tab 1", CONFIG)

    def opener(settings, spreadsheet_id=None):
        if spreadsheet_id == "bad":
            raise RuntimeError("a Sheets outage")
        return good

    sync.sync_cycle(conn, opener)
    assert len(shadow_jobs(conn)) == 1
    assert conn.execute("SELECT count(*) AS n FROM heartbeats WHERE name = 'sync'").fetchone()["n"] == 1


def test_google_connection_is_read_only(mocker):
    connect = mocker.patch("auto_archiver.job_queue.sync.gspread.service_account")
    sync.open_spreadsheet({"service_account": "account.json", "sheet_id": "spreadsheet1", "sheet": None})
    scopes = connect.call_args.kwargs["scopes"]
    assert scopes and all(scope.endswith(".readonly") for scope in scopes)
