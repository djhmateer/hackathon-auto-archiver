"""The sync rules, with no network or database"""

from auto_archiver.job_queue.sync import ADD, ALERT, CANCEL, REJECT, Row, RowState, decide, tab_folder

URL = "https://example.com/post/1"


def kinds(actions) -> list[str]:
    return [action.kind for action in actions]


# rule 1


def test_blank_row_with_nothing_known_gets_a_job():
    action = decide([Row(2, URL)], {})[0]
    assert (action.kind, action.row, action.url, action.domain) == (ADD, 2, URL, "example.com")


def test_none_status_counts_as_blank():
    assert kinds(decide([Row(2, URL, status=None)], {})) == [ADD]


def test_url_with_no_scheme_or_stray_punctuation_gets_its_domain():
    assert decide([Row(2, "example.com/page")], {})[0].domain == "example.com"
    assert decide([Row(2, ": https://example.com/page")], {})[0].domain == "example.com"


def test_result_written_during_the_read_is_not_archived_again():
    assert decide([Row(2, URL)], {2: RowState(written_since_snapshot=True)}) == []


# rule 2


def test_blank_row_with_a_running_job_is_left_alone():
    assert decide([Row(2, URL)], {2: RowState(job_id=1, job_status="running", job_url=URL)}) == []


def test_blank_row_with_a_queued_job_is_left_alone():
    assert decide([Row(2, URL)], {2: RowState(job_id=1, job_status="queued", job_url=URL)}) == []


def test_blank_row_with_an_unwritten_result_is_left_alone():
    assert decide([Row(2, URL)], {2: RowState(has_unwritten=True)}) == []


# rule 3


def test_queued_status_with_no_job_raises_an_alert():
    assert kinds(decide([Row(2, URL, status="Queued")], {})) == [ALERT]


def test_in_progress_status_with_no_job_raises_an_alert():
    assert kinds(decide([Row(2, URL, status="Archive in progress")], {})) == [ALERT]


def test_in_progress_status_with_a_job_or_unwritten_entry_is_fine():
    running = RowState(job_id=1, job_status="running", job_url=URL)
    assert decide([Row(2, URL, status="Archive in progress")], {2: running}) == []
    assert decide([Row(2, URL, status="Archive in progress")], {2: RowState(has_unwritten=True)}) == []


def test_no_alert_if_the_result_was_written_after_the_read():
    assert decide([Row(2, URL, status="Archive in progress")], {2: RowState(written_since_snapshot=True)}) == []


# rule 4


def test_any_other_status_is_left_alone():
    for status in ("Archive failed - a reason", "Problem in archive - run again", "success"):
        assert decide([Row(2, URL, status=status)], {}) == []


# rule 5


def test_queued_job_with_a_changed_url_is_cancelled_and_nothing_added_this_cycle():
    actions = decide([Row(2, "https://example.com/other")], {2: RowState(job_id=7, job_status="queued", job_url=URL)})
    assert [(a.kind, a.row, a.job_id) for a in actions] == [(CANCEL, 2, 7)]


def test_queued_job_whose_row_has_gone_is_cancelled():
    actions = decide([], {2: RowState(job_id=7, job_status="queued", job_url=URL)})
    assert [(a.kind, a.row, a.job_id) for a in actions] == [(CANCEL, 2, 7)]


def test_running_job_is_never_cancelled():
    running = RowState(job_id=7, job_status="running", job_url=URL)
    assert decide([], {2: running}) == []
    assert decide([Row(2, "https://example.com/other", status="Archive in progress")], {2: running}) == []


# the feeder's checks


def test_invalid_urls_are_rejected_like_the_feeder():
    for url in ("N/A", "-", "https://[bad"):
        action = decide([Row(2, url)], {})[0]
        assert (action.kind, action.message) == (REJECT, "WARNING: not a valid URL")


def test_missing_folder_is_rejected_only_when_required():
    action = decide([Row(2, URL)], {}, must_have_folder=True)[0]
    assert (action.kind, action.message) == (REJECT, "WARNING:Folder Name not set")
    assert kinds(decide([Row(2, URL, folder="  ")], {}, must_have_folder=True)) == [REJECT]
    assert kinds(decide([Row(2, URL, folder="a folder")], {}, must_have_folder=True)) == [ADD]


def test_rejected_row_with_an_unwritten_warning_is_not_rejected_again():
    assert decide([Row(2, "N/A")], {2: RowState(has_unwritten=True)}) == []


# folders


def test_folder_matches_the_feeder():
    assert tab_folder("My Folder ", "My Sheet", "Tab 1", True) == "my-folder/my-sheet/tab-1"
    assert tab_folder("My Folder", "My Sheet", "Tab 1", False) == "my-folder"
    assert tab_folder("", "My Sheet", "Tab 1", True) == ""
    assert tab_folder(None, "My Sheet", "Tab 1", True) == ""


def test_add_carries_the_folder():
    action = decide([Row(2, URL, folder="My Folder")], {}, "My Sheet", "Tab 1", True)[0]
    assert action.folder == "my-folder/my-sheet/tab-1"
