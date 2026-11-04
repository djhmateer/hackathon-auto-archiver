"""The sheet cells of a result, split out of GsheetsFeederDB.done() so the queue can use them with no sheet access"""

import json

from auto_archiver.core import Media, Metadata
from auto_archiver.modules.gsheet_feeder_db.gsheet_feeder_db import build_result_cells, cells_to_write


def test_builds_every_cell_without_a_sheet(mocker):
    mocker.patch(
        "auto_archiver.modules.gsheet_feeder_db.gsheet_feeder_db.get_current_timestamp",
        return_value="2025-02-01T00:00:00+00:00",
    )
    item = Metadata().set_url("http://example.com/post").success("my-archiver")
    item.set_title("A title").set_content("Some text").set("timestamp", "2025-01-01T00:00:00")
    item.add_media(Media(filename="s.png", urls=["http://example.com/s.png"]), id="screenshot")
    item.add_media(Media(filename="thumbnail.png", urls=["http://example.com/t.png"]), id="thumbnail")
    wacz = Media(filename="a.wacz", urls=["http://example.com/a.wacz"])
    item.add_media(wacz, id="browsertrix")
    final = Media(filename="final.html", urls=["http://example.com/final.html"])
    final.set("hash", "abc")
    item.set_final_media(final)

    cells = build_result_cells(item)

    assert list(cells)[0] == "status"
    assert cells["status"] == "my-archiver: success"
    assert cells["archive"] == "http://example.com/final.html"
    assert cells["date"] == "2025-02-01T00:00:00+00:00"
    assert cells["title"] == "A title"
    assert cells["text"] == "Some text"
    assert cells["timestamp"] == "2025-01-01T00:00:00+00:00"
    assert cells["hash"] == "abc"
    assert cells["screenshot"] == "http://example.com/s.png"
    assert cells["wacz"] == "http://example.com/a.wacz"
    assert "replayweb.page" in cells["replaywebpage"]
    json.dumps(cells)  # they go in sheet_writes as jsonb


def test_graphic_rating_and_images_summary():
    item = Metadata().set_url("http://example.com/post").success("my-archiver")
    item.set("graphic_rating", 4).set("images_summary", "Two images, one shows a body.")

    cells = build_result_cells(item)

    assert cells["graphic_rating"] == 4
    assert cells["images_summary"] == "Two images, one shows a body."


def test_cached_notes_and_failed_uploads_go_in_the_status():
    item = Metadata().set_url("http://example.com/post").success("my-archiver")
    item.record_store_failure("a.mp4")
    item.add_status_note("a note")
    assert build_result_cells(item, cached=True)["status"] == (
        "[cached] my-archiver: success (1 upload failed - see logs) (a note)"
    )


def test_empty_values_are_left_out():
    cells = build_result_cells(Metadata().set_url("http://example.com/post").success("my-archiver"))
    assert not {"archive", "title", "text", "hash", "screenshot", "thumbnail", "wacz"} & set(cells)


def test_status_is_always_written_even_if_empty():
    assert cells_to_write({"status": ""}, lambda col: False, lambda col: "x", False) == {"status": ""}


def test_missing_columns_and_filled_cells_are_skipped():
    cells = {"status": "ok", "title": "New", "text": "New", "hash": "New"}
    to_write = cells_to_write(cells, lambda col: col != "hash", lambda col: "old" if col == "title" else "", False)
    assert to_write == {"status": "ok", "text": "New"}


def test_overwriting_replaces_filled_cells():
    to_write = cells_to_write({"status": "ok", "title": "New"}, lambda col: True, lambda col: "old", True)
    assert to_write == {"status": "ok", "title": "New"}


def test_a_failure_on_one_cell_keeps_the_others(mocker):
    def existing(col):
        if col == "title":
            raise ValueError("a bad cell")
        return ""

    to_write = cells_to_write({"status": "ok", "title": "New", "text": "New"}, lambda col: True, existing, False)
    assert to_write == {"status": "ok", "text": "New"}
