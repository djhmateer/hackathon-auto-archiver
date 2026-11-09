"""
queue_feeder_db, then the supervisor running the real archiver as a real child process against real Postgres.
The child uses a throwaway extractor that makes no network calls, and no cookies, sessions or real sites are involved.
"""

import hashlib
import time

import psycopg
import pytest

from auto_archiver.core import Media, Metadata
from auto_archiver.job_queue import db
from auto_archiver.job_queue.worker_supervisor import WorkerSupervisor, start_child
from auto_archiver.modules.queue_feeder_db import queue_feeder_db
from auto_archiver.modules.queue_feeder_db.queue_feeder_db import QueueFeederDB

CONFIG = "orchestration-example.yaml"
URL = "https://example.com/post/1"


@pytest.fixture
def module(conn, monkeypatch, database_url):
    feeder = QueueFeederDB.__new__(QueueFeederDB)
    return feeder


def add_claimed_job(conn, module, folder="", url=URL):
    sheet = conn.execute(
        """
        INSERT INTO sheets (spreadsheet_id, worksheet_gid, worksheet, config, enabled)
        VALUES ('spreadsheet1', 1, 'tab1', %s, true) RETURNING id
        """,
        (CONFIG,),
    ).fetchone()["id"]
    job_id = db.add_job(conn, sheet, 2, url, "example.com", folder)
    conn.execute("UPDATE jobs SET available_at = now() - interval '1 minute' WHERE id = %s", (job_id,))
    job = db.claim_job(conn, "vm1")
    module.job_id, module.lease_token = job_id, str(job["lease_token"])
    return job


def test_yields_the_job_and_marks_work_started(conn, module):
    job = add_claimed_job(conn, module, folder="a-folder")
    items = list(module)
    assert [i.get_url() for i in items] == [URL]
    assert items[0].get_context("folder") == "a-folder"
    assert db.get_job(conn, job["id"])["work_started_at"] is not None


def test_no_folder_sets_no_context(conn, module):
    add_claimed_job(conn, module)
    assert list(module)[0].get_context("folder", "none") == "none"


def test_yields_nothing_with_a_stale_token(conn, module):
    job = add_claimed_job(conn, module)
    conn.execute("UPDATE jobs SET lease_token = gen_random_uuid() WHERE id = %s", (job["id"],))
    assert list(module) == []
    assert db.get_job(conn, job["id"])["work_started_at"] is None


def test_started_logs_the_line_the_timings_use_and_writes_nothing(conn, module, caplog):
    job = add_claimed_job(conn, module)
    list(module)
    entries = len(conn.execute("SELECT * FROM sheet_writes WHERE job_id = %s", (job["id"],)).fetchall())
    module.started(Metadata().set_url(URL))
    assert "STARTED" in caplog.text
    assert len(conn.execute("SELECT * FROM sheet_writes WHERE job_id = %s", (job["id"],)).fetchall()) == entries


def test_done_saves_the_result_cells(conn, module, caplog):
    job = add_claimed_job(conn, module)
    list(module)
    item = Metadata().set_url(URL).success("my-archiver")
    item.set_title("A title")
    module.done(item)
    assert "DONE" in caplog.text
    assert db.get_job(conn, job["id"])["status"] == "done"
    cells = conn.execute("SELECT cells FROM sheet_writes ORDER BY id DESC LIMIT 1").fetchone()["cells"]
    assert cells["status"] == "my-archiver: success"
    assert cells["title"] == "A title"


def finish_blocked(conn, module, url, note):
    add_claimed_job(conn, module, url=url)
    list(module)
    item = Metadata().set_url(url)
    item.status = "no archiver"
    item.add_status_note(note)
    module.done(item)
    return conn.execute("SELECT kind, message FROM admin_alerts").fetchall()


@pytest.mark.parametrize("url", ["https://www.idf.il/123456", "https://idf.il/123456"])
def test_a_block_asking_for_a_requeue_on_idf_il_raises_an_alert(conn, module, url):
    alerts = finish_blocked(conn, module, url, "blocked by site - please requeue")
    assert [a["kind"] for a in alerts] == ["site_blocked"]
    first_line, rest = alerts[0]["message"].split("\n", 1)
    assert first_line == "idf.il blocked tab tab1 row 2. Clear the row's status to archive it again"
    assert f"URL: {url}" in rest
    assert "Status: no archiver (blocked by site - please requeue)" in rest


@pytest.mark.parametrize(
    "url, note",
    [
        # another site
        (URL, "blocked by site - please requeue"),
        # a block that a requeue won't get past
        ("https://www.idf.il/123456", "blocked by site - HTTP 403"),
        ("https://www.idf.il/123456", "screenshot blocked by site"),
    ],
)
def test_no_alert_for_other_sites_or_blocks(conn, module, url, note):
    assert finish_blocked(conn, module, url, note) == []


def test_done_saves_the_stats_and_each_file(conn, module, tmp_path):
    job = add_claimed_job(conn, module)
    list(module)
    video, page = tmp_path / "video.mp4", tmp_path / "page.html"
    video.write_bytes(b"v" * 1000)
    page.write_bytes(b"<html></html>")
    item = Metadata().set_url(URL).success("vimeo")
    item.set_title("A title")
    item.set_timestamp("2026-01-01T12:00:00")
    stored = Media(str(video)).set("hash", "SHA-256:abc").set("duration", "61.5")
    stored._key, stored.urls = "folder1/video.mp4", ["https://example.com/folder1/video.mp4"]
    item.add_media(stored)
    item.set_final_media(Media(str(page)))
    module.done(item)

    current = db.get_job(conn, job["id"])
    assert (current["title"], current["extractor"], current["cached"], current["store_failures"]) == (
        "A title",
        "vimeo",
        False,
        0,
    )
    assert current["posted_at"].isoformat() == "2026-01-01T12:00:00+00:00"
    files = conn.execute("SELECT * FROM job_files WHERE job_id = %s ORDER BY id", (job["id"],)).fetchall()
    assert [(f["role"], f["key"], f["mimetype"], f["bytes"], f["attempt"]) for f in files] == [
        ("", "folder1/video.mp4", "video/mp4", 1000, 1),
        ("_final_media", None, "text/html", 13, 1),
    ]
    assert (files[0]["hash"], float(files[0]["duration_seconds"])) == ("SHA-256:abc", 61.5)
    assert files[1]["hash"].startswith("SHA-256:") and files[1]["duration_seconds"] is None


def test_a_missed_file_gets_the_enrichers_algorithm_and_a_wacz_its_mimetype(tmp_path):
    video, wacz = tmp_path / "video.mp4", tmp_path / "crawl.wacz"
    video.write_bytes(b"v")
    wacz.write_bytes(b"w")
    item = Metadata().set_url(URL)
    item.add_media(Media(str(video)).set("hash", "SHA3-512:abc"))
    item.add_media(Media(str(wacz)), id="browsertrix")
    files = queue_feeder_db.describe_files(item)
    assert files[1]["mimetype"] == "application/wacz"
    assert files[1]["hash"] == f"SHA3-512:{hashlib.sha3_512(b'w').hexdigest()}"


def test_done_still_saves_the_result_if_the_stats_fail(conn, module, mocker):
    job = add_claimed_job(conn, module)
    list(module)
    mocker.patch.object(queue_feeder_db, "describe_files", side_effect=ValueError("a bug"))
    module.done(Metadata().set_url(URL).success("vimeo"))
    assert db.get_job(conn, job["id"])["status"] == "done"


def test_failed_saves_the_reason(conn, module):
    job = add_claimed_job(conn, module)
    list(module)
    module.failed(Metadata().set_url(URL), "- a reason")
    current = db.get_job(conn, job["id"])
    assert (current["status"], current["error"]) == ("failed", "- a reason")
    cells = conn.execute("SELECT cells FROM sheet_writes ORDER BY id DESC LIMIT 1").fetchone()["cells"]
    assert cells["status"] == "Archive failed - a reason"
    assert "date" in cells


def test_a_replaced_worker_saves_nothing(conn, module):
    job = add_claimed_job(conn, module)
    list(module)
    conn.execute("UPDATE jobs SET lease_token = gen_random_uuid() WHERE id = %s", (job["id"],))
    module.done(Metadata().set_url(URL).success("old"))
    assert db.get_job(conn, job["id"])["status"] == "running"


def test_retries_when_postgres_is_unreachable(conn, module, mocker):
    job = add_claimed_job(conn, module)
    list(module)
    mocker.patch.object(queue_feeder_db.time, "sleep")
    real_connect = db.connect
    failures = [psycopg.OperationalError("Postgres has gone")] * 2

    def flaky():
        if failures:
            raise failures.pop()
        return real_connect()

    mocker.patch("auto_archiver.job_queue.db.connect", side_effect=flaky)
    module.done(Metadata().set_url(URL).success("my-archiver"))
    assert db.get_job(conn, job["id"])["status"] == "done"


def test_gives_up_in_the_end(conn, module, mocker):
    add_claimed_job(conn, module)
    list(module)
    mocker.patch.object(queue_feeder_db.time, "sleep")
    mocker.patch("auto_archiver.job_queue.db.connect", side_effect=psycopg.OperationalError("Postgres has gone"))
    with pytest.raises(psycopg.OperationalError):
        module.done(Metadata().set_url(URL).success("my-archiver"))


def test_other_errors_are_not_retried(conn, module, mocker):
    add_claimed_job(conn, module)
    list(module)
    sleep = mocker.patch.object(queue_feeder_db.time, "sleep")
    mocker.patch("auto_archiver.job_queue.db.finish_job", side_effect=ValueError("a bug"))
    with pytest.raises(ValueError):
        module.done(Metadata().set_url(URL).success("my-archiver"))
    sleep.assert_not_called()


# the real thing, end to end

EXTRACTOR_MANIFEST = """{
    "name": "fake_extractor",
    "type": ["extractor"],
    "entry_point": "fake_extractor::FakeExtractor",
    "requires_setup": False,
    "dependencies": {"python": []},
    "description": "For tests. Makes no network calls.",
}
"""
EXTRACTOR_CODE = """
import os
from auto_archiver.core import Extractor, Media, Metadata

class FakeExtractor(Extractor):
    def download(self, item):
        if "fail" in item.get_url():
            raise AssertionError("a row that fails")
        path = os.path.join(self.tmp_dir, "file.txt")
        with open(path, "w") as f:
            f.write("archived in " + os.path.abspath(self.tmp_dir) + " for folder " + item.get_context("folder", ""))
        result = Metadata().set_url(item.get_url()).success("fake_extractor")
        result.set_title("A fake title")
        result.add_media(Media(path))
        return result
"""
ORCHESTRATION = """steps:
  feeders: [gsheet_feeder_db]
  extractors: [fake_extractor]
  enrichers: [hash_enricher]
  databases: [gsheet_feeder_db]
  storages: [local_storage]
  formatters: [mute_formatter]
logging:
  level: DEBUG
local_storage:
  save_to: {save_to}
"""


@pytest.fixture
def run_for_real(conn, tmp_path, monkeypatch, database_url):
    """Runs the supervisor with real children until the given jobs have finished or been requeued"""
    monkeypatch.chdir(tmp_path)
    module_dir = tmp_path / "test_modules" / "fake_extractor"
    module_dir.mkdir(parents=True)
    (module_dir / "__init__.py").write_text("from .fake_extractor import FakeExtractor\n")
    (module_dir / "__manifest__.py").write_text(EXTRACTOR_MANIFEST)
    (module_dir / "fake_extractor.py").write_text(EXTRACTOR_CODE)
    (tmp_path / CONFIG).write_text(ORCHESTRATION.format(save_to=tmp_path / "stored"))
    sheet = conn.execute(
        """
        INSERT INTO sheets (spreadsheet_id, worksheet_gid, worksheet, config, enabled)
        VALUES ('spreadsheet1', 1, 'tab1', %s, true) RETURNING id
        """,
        (CONFIG,),
    ).fetchone()["id"]

    def start(job, worker_dir):
        return start_child(job, worker_dir, ("--module_paths", str(tmp_path / "test_modules")))

    def run(urls, config=None):
        if config:
            (tmp_path / CONFIG).write_text(config)
        job_ids = []
        for number, url in enumerate(urls):
            job_ids.append(db.add_job(conn, sheet, number + 2, url, "example.com", "a-folder"))
        conn.execute("UPDATE jobs SET available_at = now() - interval '1 minute'")
        sup = WorkerSupervisor("vm1", start=start)
        sup.clear_leftovers()
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            sup.tick(conn)
            waiting = conn.execute(
                "SELECT count(*) AS n FROM jobs WHERE status = 'running' OR (status = 'queued' AND available_at <= now())"
            ).fetchone()["n"]
            if waiting == 0 and sup.is_idle():
                return sup, job_ids
            time.sleep(0.2)
        if sup.running:
            sup.running.process.kill()
        output = (tmp_path / "logs" / "console" / f"job_{job_ids[-1]}.log").read_text()[-3000:]
        pytest.fail(f"The jobs didn't finish. The child's output:\n{output}")

    return run


def test_real_child_archives_a_job_end_to_end(conn, run_for_real, tmp_path):
    sup, (job_id,) = run_for_real(["https://example.com/post/1"])

    current = db.get_job(conn, job_id)
    assert current["status"] == "done", (tmp_path / "logs" / "console" / f"job_{job_id}.log").read_text()[-3000:]
    assert current["work_started_at"] is not None
    # every line of the archiver's debug log says which job it's for
    debug_lines = (tmp_path / "logs" / "1debug.log").read_text().splitlines()
    assert any(line.startswith("20") for line in debug_lines)
    assert all(f" | job {job_id} | " in line for line in debug_lines if line.startswith("20"))
    cells = conn.execute("SELECT cells FROM sheet_writes ORDER BY id DESC LIMIT 1").fetchone()["cells"]
    assert cells["status"] == "fake_extractor: success"
    assert cells["title"] == "A fake title"

    # logs in logs/, with the lines the timings use
    debug_log = (tmp_path / "logs" / "1debug.log").read_text()
    assert "STARTED" in debug_log
    assert "DONE https://example.com/post/1" in debug_log
    assert (tmp_path / "logs" / "hangs.log").exists()

    # the job's folder reached the child, and files were worked on under the temp folder
    stored = list((tmp_path / "stored").rglob("*.txt"))
    assert len(stored) == 1
    assert str(tmp_path / "tmp") in stored[0].read_text()
    assert "for folder a-folder" in stored[0].read_text()
    assert not (tmp_path / "tmp").exists(), "wiped afterwards"


def test_real_child_with_a_failing_row_reports_it(conn, run_for_real):
    _, (job_id,) = run_for_real(["https://example.com/fail"])
    # the extractor's error is caught by the orchestrator, so the row ends as nothing archived, as under cron
    assert db.get_job(conn, job_id)["status"] == "done"
    cells = conn.execute("SELECT cells FROM sheet_writes ORDER BY id DESC LIMIT 1").fetchone()["cells"]
    assert "success" not in cells["status"]


def test_real_child_with_a_bad_config_is_a_setup_failure(conn, run_for_real, tmp_path):
    bad_config = ORCHESTRATION.format(save_to=tmp_path / "stored").replace("fake_extractor", "no_such_extractor")
    _, (job_id,) = run_for_real(["https://example.com/post/1"], config=bad_config)
    current = db.get_job(conn, job_id)
    assert (current["status"], current["attempts"], current["work_started_at"]) == ("queued", 0, None)
    assert conn.execute("SELECT setup_failures FROM sheets").fetchone()["setup_failures"] == 1
    console = (tmp_path / "logs" / "console" / f"job_{job_id}.log").read_text()
    header, output = console.split("\n", 1)
    assert header.startswith(f"===== job {job_id} attempt 1 started ")
    assert output.strip() != ""  # the setup error


def test_real_child_killed_mid_job_has_its_processes_and_files_cleared(conn, run_for_real, tmp_path, mocker):
    # the fake child sleeps once archiving starts, then the supervisor's time limit fires
    code = EXTRACTOR_CODE.replace(
        'path = os.path.join(self.tmp_dir, "file.txt")',
        'open(os.path.join(self.tmp_dir, "started"), "w").close()\n        import time; time.sleep(120)\n'
        '        path = os.path.join(self.tmp_dir, "file.txt")',
    )
    (tmp_path / "test_modules" / "fake_extractor" / "fake_extractor.py").write_text(code)
    limit = mocker.patch("auto_archiver.job_queue.worker_supervisor.TIME_LIMIT_SECONDS", 25)
    assert limit == 25

    real_tick = WorkerSupervisor.tick

    def tick_and_check_limits(self, conn):
        self.check_time_limits()
        real_tick(self, conn)

    mocker.patch.object(WorkerSupervisor, "tick", tick_and_check_limits)
    _, (job_id,) = run_for_real(["https://example.com/post/1"])

    current = db.get_job(conn, job_id)
    assert current["status"] == "failed", (tmp_path / "logs" / "console" / f"job_{job_id}.log").read_text()[-3000:]
    assert "killed" in current["error"]
    assert not (tmp_path / "tmp").exists()
