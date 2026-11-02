"""The writer, live sync and the whole path of a row, against real Postgres and a fake spreadsheet"""

import pytest
from gspread import utils as gspread_utils
from job_queue_fakes import FakeSpreadsheet, FakeTab, opener_for

from auto_archiver.job_queue import db, sync, writer

HEADERS = ["Link", "Archive status", "Destination folder", "Upload title", "Archive location", "Thumbnail"]
CONFIG = "orchestration-example.yaml"
URL = "https://example.com/post/1"


def row(url=URL, status="", folder="", title="", archive=""):
    return [url, status, folder, title, archive, ""]


@pytest.fixture
def config(tmp_path, monkeypatch):
    def write(**settings):
        lines = "".join(f"  {key}: {value}\n" for key, value in settings.items())
        (tmp_path / CONFIG).write_text(f"gsheet_feeder_db:\n  sheet_id: spreadsheet1\n{lines}")

    monkeypatch.chdir(tmp_path)
    write()
    return write


@pytest.fixture
def tab():
    return FakeTab(1, "Tab 1", [list(HEADERS), row()])


@pytest.fixture
def sheet(tab):
    return FakeSpreadsheet([tab])


@pytest.fixture
def sheet_id(conn, config, sheet):
    sync.register(conn, CONFIG, opener_for(sheet))
    conn.execute("UPDATE sheets SET enabled = true")
    return db.get_sheets(conn)[0]["id"]


def run_sync(conn, sheet):
    sync.sync_cycle(conn, opener_for(sheet))


def run_writer(conn, sheet):
    return writer.writer_cycle(conn, opener_for(sheet))


def cell(tab, a1):
    r, c = gspread_utils.a1_to_rowcol(a1)
    return tab.rows[r - 1][c - 1]


def jobs(conn):
    conn.execute("UPDATE jobs SET available_at = now() - interval '1 minute' WHERE status = 'queued'")
    return conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()


def conflicts(conn):
    return db.get_conflicts(conn)


def claim(conn):
    jobs(conn)  # the clock on WSL2 can step back, which makes a job just added look not yet available
    return db.claim_job(conn, "vm1")


def add_entry_job(conn, sheet_id, row_number=2, url=URL):
    return db.add_job(conn, sheet_id, row_number, url, "example.com")


# the whole path of a row


def test_a_row_from_blank_through_to_its_result(conn, sheet_id, sheet, tab):
    run_sync(conn, sheet)
    run_writer(conn, sheet)
    assert cell(tab, "B2") == "Queued"

    job = claim(conn)
    assert job["url"] == URL
    run_writer(conn, sheet)
    assert cell(tab, "B2") == "Archive in progress"

    run_sync(conn, sheet)
    assert len(jobs(conn)) == 1, "a running job is left alone"
    assert db.get_row_states(conn, sheet_id, db.now(conn))[0]["job_status"] == "running"

    db.finish_job(conn, job["id"], job["lease_token"], "done", {"status": "ok", "title": "A title"})
    run_sync(conn, sheet)
    assert len(jobs(conn)) == 1, "the sheet still says in progress, and there's no alert"
    assert conn.execute("SELECT count(*) AS n FROM alerts").fetchone()["n"] == 0

    run_writer(conn, sheet)
    assert cell(tab, "B2") == "ok"
    assert cell(tab, "D2") == "A title"
    assert conflicts(conn) == []

    run_sync(conn, sheet)
    assert len(jobs(conn)) == 1, "a finished row isn't archived again"


def test_client_clearing_a_finished_row_gets_a_new_job(conn, sheet_id, sheet, tab):
    run_sync(conn, sheet)
    job = claim(conn)
    db.finish_job(conn, job["id"], job["lease_token"], "done", {"status": "ok"})
    run_writer(conn, sheet)

    tab.rows[1][1] = ""
    # a read from before the result was written would look the same: the snapshot check has to tell them apart
    conn.execute("UPDATE outbox SET written_at = now() - interval '1 minute'")
    run_sync(conn, sheet)
    assert len(jobs(conn)) == 2


def test_one_cycle_takes_a_finished_job_through_all_its_entries_in_order(conn, sheet_id, sheet, tab):
    run_sync(conn, sheet)
    job = claim(conn)
    db.finish_job(conn, job["id"], job["lease_token"], "done", {"status": "ok"})
    assert run_writer(conn, sheet) == 3
    assert cell(tab, "B2") == "ok"
    statuses = [w[0][0]["values"][0][0] for w in tab.writes]
    assert statuses == ["Queued", "Archive in progress", "ok"]


# sync's own rules, live


def test_result_written_during_the_read_is_not_archived_twice(conn, sheet_id, sheet, tab, mocker):
    """The race: sync reads a blank status, then the result is written before sync checks Postgres"""
    job_id = add_entry_job(conn, sheet_id)
    run_writer(conn, sheet)
    job = claim(conn)
    run_writer(conn, sheet)
    db.finish_job(conn, job["id"], job["lease_token"], "done", {"status": "ok"})
    tab.rows[1][1] = ""  # how the sheet looked when it was read, before the result arrived

    real_read = sync.read_tabs

    def read_then_write(*args):
        rows = real_read(*args)
        run_writer(conn, FakeSpreadsheet([FakeTab(1, "Tab 1", [HEADERS, row(status="Archive in progress")])]))
        return rows

    mocker.patch("auto_archiver.job_queue.sync.read_tabs", side_effect=read_then_write)
    run_sync(conn, sheet)
    assert [j["id"] for j in jobs(conn)] == [job_id]


def test_rejected_row_gets_a_warning_once_and_never_runs(conn, sheet_id, sheet, tab):
    tab.rows[1] = row(url="N/A")
    run_sync(conn, sheet)
    run_sync(conn, sheet)
    assert [j["status"] for j in jobs(conn)] == ["rejected"]
    assert claim(conn) is None
    run_writer(conn, sheet)
    assert cell(tab, "B2") == "WARNING: not a valid URL"
    run_sync(conn, sheet)
    assert len(jobs(conn)) == 1


def test_missing_folder_is_rejected_when_required(conn, config, sheet_id, sheet, tab):
    config(must_have_folder_name_for_archive_to_run="true")
    run_sync(conn, sheet)
    run_writer(conn, sheet)
    assert cell(tab, "B2") == "WARNING:Folder Name not set"


def test_job_gets_the_final_folder(conn, config, sheet_id, sheet, tab):
    config(use_sheet_names_in_stored_paths="false")
    tab.rows[1] = row(folder="My Folder")
    run_sync(conn, sheet)
    assert jobs(conn)[0]["folder"] == "my-folder"


def test_changed_url_cancels_clears_then_adds_the_new_one(conn, sheet_id, sheet, tab):
    new_url = "https://example.com/new"
    run_sync(conn, sheet)
    run_writer(conn, sheet)
    tab.rows[1][0] = new_url

    run_sync(conn, sheet)
    assert [j["status"] for j in jobs(conn)] == ["cancelled"]

    run_writer(conn, sheet)
    assert cell(tab, "B2") == ""
    assert conflicts(conn) == []

    # the clear was written after this pass's snapshot only if they overlap, so it may take one more pass
    conn.execute("UPDATE outbox SET written_at = now() - interval '1 minute'")
    run_sync(conn, sheet)
    assert [(j["status"], j["url"]) for j in jobs(conn)] == [("cancelled", URL), ("queued", new_url)]


def test_disagreement_alerts_only_once_a_day(conn, sheet_id, sheet, tab):
    tab.rows[1] = row(status="Queued")
    run_sync(conn, sheet)
    run_sync(conn, sheet)
    alerts = conn.execute("SELECT * FROM alerts").fetchall()
    assert [a["kind"] for a in alerts] == ["sheet_disagrees"]


def test_shadow_and_live_tabs_stay_separate(conn, config, sheet):
    live, shadow = FakeTab(1, "Live", [HEADERS, row()]), FakeTab(2, "Shadow", [HEADERS, row()])
    both = FakeSpreadsheet([live, shadow])
    sync.register(conn, CONFIG, opener_for(both))
    conn.execute("UPDATE sheets SET enabled = true WHERE worksheet = 'Live'")
    run_sync(conn, both)
    assert len(jobs(conn)) == 1
    assert len(conn.execute("SELECT * FROM shadow_jobs").fetchall()) == 1


# the writer's sanity check


def test_changed_url_is_a_conflict_and_writes_nothing(conn, sheet_id, sheet, tab):
    add_entry_job(conn, sheet_id)
    tab.rows[1][0] = "https://example.com/other"
    assert run_writer(conn, sheet) == 0
    assert tab.writes == []
    assert "the URL has changed" in conflicts(conn)[0]["conflict"]
    assert [a["kind"] for a in conn.execute("SELECT * FROM alerts").fetchall()] == ["writer_conflict"]


def test_unexpected_status_is_a_conflict_and_writes_nothing(conn, sheet_id, sheet, tab):
    add_entry_job(conn, sheet_id)
    tab.rows[1][1] = "a client typed this"
    assert run_writer(conn, sheet) == 0
    assert tab.writes == []
    assert "a client typed this" in conflicts(conn)[0]["conflict"]


def test_later_entries_wait_behind_a_conflict(conn, sheet_id, sheet, tab):
    add_entry_job(conn, sheet_id)
    job = claim(conn)
    db.finish_job(conn, job["id"], job["lease_token"], "done", {"status": "ok"})
    tab.rows[1][1] = "a client typed this"
    run_writer(conn, sheet)
    run_writer(conn, sheet)
    assert tab.writes == []
    assert len(conflicts(conn)) == 1


def test_conflict_stays_until_someone_retries_it(conn, sheet_id, sheet, tab):
    add_entry_job(conn, sheet_id)
    tab.rows[1][1] = "a client typed this"
    run_writer(conn, sheet)
    tab.rows[1][1] = ""  # the sheet looks right again, but a person decides
    assert run_writer(conn, sheet) == 0
    assert tab.writes == []
    assert len(conn.execute("SELECT * FROM alerts").fetchall()) == 1


def test_conflict_blocks_the_row_until_resolved(conn, sheet_id, sheet, tab):
    add_entry_job(conn, sheet_id)
    tab.rows[1][1] = "a client typed this"
    run_writer(conn, sheet)
    tab.rows[1][1] = ""
    run_sync(conn, sheet)
    assert len(jobs(conn)) == 1, "rule 2: the entry is still unwritten"


def test_retry_rechecks_a_conflict_once_the_sheet_is_fixed(conn, sheet_id, sheet, tab):
    add_entry_job(conn, sheet_id)
    tab.rows[1][1] = "a client typed this"
    run_writer(conn, sheet)
    entry = conflicts(conn)[0]

    tab.rows[1][1] = ""
    assert db.retry_conflict(conn, entry["id"]) is True
    assert run_writer(conn, sheet) == 1
    assert cell(tab, "B2") == "Queued"
    assert conflicts(conn) == []


def test_discard_lets_later_entries_go_on(conn, sheet_id, sheet, tab):
    add_entry_job(conn, sheet_id)
    tab.rows[1][1] = "a client typed this"
    run_writer(conn, sheet)
    entry = conflicts(conn)[0]
    assert db.discard_conflict(conn, entry["id"]) is True
    assert conflicts(conn) == []
    assert db.get_next_outbox_entries(conn) == []


def test_retry_and_discard_need_a_conflict(conn, sheet_id):
    add_entry_job(conn, sheet_id)
    entry = db.get_next_outbox_entries(conn)[0]
    assert db.retry_conflict(conn, entry["id"]) is False
    assert db.discard_conflict(conn, entry["id"]) is False


def test_writer_stopping_before_recording_a_write_is_not_a_conflict(conn, sheet_id, sheet, tab):
    add_entry_job(conn, sheet_id)
    tab.rows[1][1] = "Queued"  # it reached the sheet, but the writer stopped before setting written_at
    assert run_writer(conn, sheet) == 1
    assert tab.writes == []
    assert conflicts(conn) == []


def test_sheet_write_failing_leaves_the_entry_to_try_again(conn, sheet_id, sheet, tab, mocker):
    add_entry_job(conn, sheet_id)
    mocker.patch.object(tab, "batch_update", side_effect=RuntimeError("a Sheets outage"))
    assert run_writer(conn, sheet) == 0
    assert len(db.get_next_outbox_entries(conn)) == 1
    assert conflicts(conn) == []
    assert conn.execute("SELECT written_at FROM outbox").fetchone()["written_at"] is None

    mocker.stopall()
    assert run_writer(conn, sheet) == 1


def test_one_failing_spreadsheet_does_not_stop_another(conn, config, sheet, tab):
    good = FakeSpreadsheet([FakeTab(1, "Tab 1", [HEADERS, row()])], spreadsheet_id="good")
    db.save_tab(conn, "bad", 1, "Tab 1", CONFIG)
    db.save_tab(conn, "good", 1, "Tab 1", CONFIG)
    for s in db.get_sheets(conn):
        add_entry_job(conn, s["id"])

    def opener(settings, spreadsheet_id=None):
        if spreadsheet_id == "bad":
            raise RuntimeError("a Sheets outage")
        return good

    assert writer.writer_cycle(conn, opener) == 1
    assert cell(good.tabs[0], "B2") == "Queued"


# what gets written


def test_existing_cells_are_kept_unless_overwriting_is_allowed(conn, config, sheet_id, sheet, tab):
    tab.rows[1] = row(title="the client's title")
    job_id = add_entry_job(conn, sheet_id)
    job = claim(conn)
    db.finish_job(conn, job_id, job["lease_token"], "done", {"status": "ok", "title": "New", "archive": "a link"})
    run_writer(conn, sheet)
    assert cell(tab, "D2") == "the client's title"
    assert cell(tab, "E2") == "a link"


def test_overwrite_setting_replaces_existing_cells(conn, config, sheet_id, sheet, tab):
    config(allow_overwrite_of_spreadsheet_cells="true")
    tab.rows[1] = row(title="the client's title")
    job_id = add_entry_job(conn, sheet_id)
    job = claim(conn)
    db.finish_job(conn, job_id, job["lease_token"], "done", {"status": "ok", "title": "New"})
    run_writer(conn, sheet)
    assert cell(tab, "D2") == "New"


def test_column_the_sheet_does_not_have_is_skipped(conn, sheet_id, sheet, tab):
    job_id = add_entry_job(conn, sheet_id)
    job = claim(conn)
    db.finish_job(conn, job_id, job["lease_token"], "done", {"status": "ok", "wacz": "a link"})
    assert run_writer(conn, sheet) == 3
    assert cell(tab, "B2") == "ok"
    assert conflicts(conn) == []


def test_formulas_go_in_as_formulas_and_long_text_is_cut_to_the_limit(conn, sheet_id, sheet, tab):
    job_id = add_entry_job(conn, sheet_id)
    job = claim(conn)
    cells = {"status": "ok", "thumbnail": '=IMAGE("https://example.com/a.png")', "title": "x" * 60000}
    db.finish_job(conn, job_id, job["lease_token"], "done", cells)
    run_writer(conn, sheet)
    assert tab.writes[-1][1] == "USER_ENTERED"
    assert cell(tab, "F2") == '=IMAGE("https://example.com/a.png")'
    assert len(cell(tab, "D2")) == 49999


def test_reads_only_the_cells_it_needs(conn, sheet_id, sheet, tab):
    add_entry_job(conn, sheet_id)
    run_writer(conn, sheet)
    assert sheet.ranges_read == ["'Tab 1'!1:1", "'Tab 1'!A2", "'Tab 1'!B2"]


def test_renamed_tab_still_gets_its_results(conn, config, sheet_id, sheet, tab):
    config(allow_worksheets="['Tab 1']")
    add_entry_job(conn, sheet_id)
    tab.title = "Renamed"
    assert run_writer(conn, sheet) == 1
    assert cell(tab, "B2") == "Queued"


def test_deleted_tab_is_a_conflict(conn, sheet_id, sheet):
    add_entry_job(conn, sheet_id)
    sheet.tabs.clear()
    assert run_writer(conn, sheet) == 0
    assert "deleted" in conflicts(conn)[0]["conflict"]


def test_tab_with_a_renamed_status_column_is_a_conflict(conn, sheet_id, sheet, tab):
    add_entry_job(conn, sheet_id)
    tab.rows[0][1] = "Something else"
    assert run_writer(conn, sheet) == 0
    assert "no url or status column" in conflicts(conn)[0]["conflict"]


def test_writer_connects_read_write_and_sync_read_only(mocker):
    connect = mocker.patch("auto_archiver.job_queue.sync.gspread.service_account")
    settings = {"service_account": "account.json", "sheet_id": "spreadsheet1", "sheet": None}
    writer.open_for_writing(settings)
    assert not any(s.endswith(".readonly") for s in connect.call_args.kwargs["scopes"])
    sync.open_spreadsheet(settings)
    assert all(s.endswith(".readonly") for s in connect.call_args.kwargs["scopes"])


def test_writer_logs_a_failed_row_as_a_warning(conn, sheet_id, sheet, tab, caplog):
    run_sync(conn, sheet)
    run_writer(conn, sheet)
    job = claim(conn)
    run_writer(conn, sheet)
    status = "Archive failed - a reason - please rerun"
    db.finish_job(conn, job["id"], job["lease_token"], "failed", {"status": status})
    caplog.clear()
    run_writer(conn, sheet)
    assert cell(tab, "B2") == status
    assert [r.levelname for r in caplog.records if "wrote" in r.getMessage()] == ["WARNING"]
    assert f"row 2: wrote '{status}'" in caplog.text


# moving a spreadsheet on and off the queue


def test_cancelling_a_job_requeued_after_a_claim_clears_the_row(conn, sheet_id, sheet, tab):
    run_sync(conn, sheet)
    run_writer(conn, sheet)
    job = claim(conn)
    run_writer(conn, sheet)
    assert cell(tab, "B2") == "Archive in progress"
    db.release_setup_failure(conn, job["id"], job["lease_token"])
    assert db.cancel_job(conn, job["id"])
    run_writer(conn, sheet)
    assert conflicts(conn) == []
    assert cell(tab, "B2") == ""


def test_rollback_cancels_waiting_jobs_and_clears_their_rows(conn, sheet_id, sheet, tab):
    run_sync(conn, sheet)
    run_writer(conn, sheet)
    assert cell(tab, "B2") == "Queued"

    assert db.rollback_spreadsheet(conn, "spreadsheet1") == (1, 0, 1)
    assert claim(conn) is None
    run_writer(conn, sheet)
    assert cell(tab, "B2") == ""
    assert db.rollback_spreadsheet(conn, "spreadsheet1") == (0, 0, 0)
    run_sync(conn, sheet)
    assert jobs(conn)[0]["status"] == "cancelled" and len(jobs(conn)) == 1, "cron has the row now"


def test_rollback_waits_for_a_running_job_to_reach_the_sheet(conn, sheet_id, sheet, tab):
    run_sync(conn, sheet)
    run_writer(conn, sheet)
    job = claim(conn)
    run_writer(conn, sheet)
    assert db.rollback_spreadsheet(conn, "spreadsheet1") == (0, 1, 0)

    db.finish_job(conn, job["id"], job["lease_token"], "done", {"status": "ok"})
    assert db.rollback_spreadsheet(conn, "spreadsheet1") == (0, 0, 1)
    run_writer(conn, sheet)
    assert cell(tab, "B2") == "ok"
    assert db.rollback_spreadsheet(conn, "spreadsheet1") == (0, 0, 0)


def test_rollback_only_touches_its_own_spreadsheet(conn, sheet_id, sheet):
    run_sync(conn, sheet)
    assert db.rollback_spreadsheet(conn, "another-spreadsheet") == (0, 0, 0)
    assert db.get_sheets(conn)[0]["enabled"] is True
    assert jobs(conn)[0]["status"] == "queued"


def test_enable_turns_on_every_tab_of_that_spreadsheet_only(conn, config, sheet, tab):
    sheet.tabs.append(FakeTab(2, "Tab 2", [list(HEADERS), row()]))
    sync.register(conn, CONFIG, opener_for(sheet))
    db.save_tab(conn, "another-spreadsheet", 1, "Tab 1", CONFIG)
    assert db.enable_spreadsheet(conn, "spreadsheet1") == 2
    enabled = {s["spreadsheet_id"]: s["enabled"] for s in db.get_sheets(conn)}
    assert enabled == {"spreadsheet1": True, "another-spreadsheet": False}


def test_new_tab_on_a_moved_spreadsheet_is_archived(conn, sheet_id, sheet, tab):
    sheet.tabs.append(FakeTab(2, "New tab", [list(HEADERS), row(url="https://example.com/post/2")]))
    run_sync(conn, sheet)
    assert {j["url"] for j in jobs(conn)} == {URL, "https://example.com/post/2"}


def test_new_tab_on_a_spreadsheet_still_on_cron_stays_off(conn, config, sheet, tab):
    sync.register(conn, CONFIG, opener_for(sheet))
    sheet.tabs.append(FakeTab(2, "New tab", [list(HEADERS), row()]))
    run_sync(conn, sheet)
    assert [s["enabled"] for s in db.get_sheets(conn)] == [False, False]


def test_commands_say_when_cron_can_go_back_on(conn, sheet_id, sheet, monkeypatch, database_url, caplog):
    run_sync(conn, sheet)
    monkeypatch.setattr("sys.argv", ["sync", "rollback", "spreadsheet1"])
    sync.main()
    assert "Keep cron off" in caplog.text
    run_writer(conn, sheet)
    caplog.clear()
    sync.main()
    assert "Cron can go back on" in caplog.text

    monkeypatch.setattr("sys.argv", ["sync", "enable", "spreadsheet1"])
    sync.main()
    assert db.get_sheets(conn)[0]["enabled"] is True


def test_registering_again_updates_the_config_but_sync_does_not(conn, config, sheet, tmp_path):
    sync.register(conn, CONFIG, opener_for(sheet))
    (tmp_path / "moved.yaml").write_text((tmp_path / CONFIG).read_text())
    sync.register(conn, "moved.yaml", opener_for(sheet))
    assert db.get_sheets(conn)[0]["config"] == "moved.yaml"

    tab = sheet.tabs[0]
    db.save_tab(conn, "spreadsheet1", tab.id, "New title", CONFIG)
    current = db.get_sheets(conn)[0]
    assert (current["config"], current["worksheet"]) == ("moved.yaml", "New title")


def test_a_discarded_conflict_cannot_be_retried_or_discarded_again(conn, sheet_id, sheet, tab):
    add_entry_job(conn, sheet_id)
    tab.rows[1][1] = "a client typed this"
    run_writer(conn, sheet)
    entry = conflicts(conn)[0]["id"]
    assert db.discard_conflict(conn, entry) is True
    assert db.discard_conflict(conn, entry) is False
    assert db.retry_conflict(conn, entry) is False
