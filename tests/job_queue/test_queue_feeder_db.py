"""
queue_feeder_db, then the supervisor running the real archiver as a real child process against real Postgres.
The child uses a throwaway extractor that makes no network calls, and no cookies, sessions or real sites are involved.
"""

import time

import psycopg
import pytest

from auto_archiver.core import Metadata
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


def add_claimed_job(conn, module, folder=""):
    sheet = conn.execute(
        """
        INSERT INTO sheets (spreadsheet_id, worksheet_gid, worksheet, config, enabled)
        VALUES ('spreadsheet1', 1, 'tab1', %s, true) RETURNING id
        """,
        (CONFIG,),
    ).fetchone()["id"]
    job_id = db.add_job(conn, sheet, 2, URL, "example.com", folder)
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
    entries = len(conn.execute("SELECT * FROM outbox WHERE job_id = %s", (job["id"],)).fetchall())
    module.started(Metadata().set_url(URL))
    assert "STARTED" in caplog.text
    assert len(conn.execute("SELECT * FROM outbox WHERE job_id = %s", (job["id"],)).fetchall()) == entries


def test_done_saves_the_result_cells(conn, module, caplog):
    job = add_claimed_job(conn, module)
    list(module)
    item = Metadata().set_url(URL).success("my-archiver")
    item.set_title("A title")
    module.done(item)
    assert "DONE" in caplog.text
    assert db.get_job(conn, job["id"])["status"] == "done"
    cells = conn.execute("SELECT cells FROM outbox ORDER BY id DESC LIMIT 1").fetchone()["cells"]
    assert cells["status"] == "my-archiver: success"
    assert cells["title"] == "A title"


def test_failed_saves_the_reason(conn, module):
    job = add_claimed_job(conn, module)
    list(module)
    module.failed(Metadata().set_url(URL), "- a reason")
    current = db.get_job(conn, job["id"])
    assert (current["status"], current["error"]) == ("failed", "- a reason")
    cells = conn.execute("SELECT cells FROM outbox ORDER BY id DESC LIMIT 1").fetchone()["cells"]
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

    def start(job, slot_dir):
        return start_child(job, slot_dir, ("--module_paths", str(tmp_path / "test_modules")))

    def run(urls, config=None):
        if config:
            (tmp_path / CONFIG).write_text(config)
        job_ids = []
        for number, url in enumerate(urls):
            job_ids.append(db.add_job(conn, sheet, number + 2, url, "example.com", "a-folder"))
        conn.execute("UPDATE jobs SET available_at = now() - interval '1 minute'")
        sup = WorkerSupervisor("vm1", 1, base_dir=str(tmp_path / "slots"), start=start)
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
        for slot in sup.slots:
            if slot.process:
                slot.process.kill()
        output = (tmp_path / "slots" / "1" / "child.out").read_text()[-3000:]
        pytest.fail(f"The jobs didn't finish. The child's output:\n{output}")

    return run


def test_real_child_archives_a_job_end_to_end(conn, run_for_real, tmp_path):
    sup, (job_id,) = run_for_real(["https://example.com/post/1"])

    current = db.get_job(conn, job_id)
    assert current["status"] == "done", (tmp_path / "slots" / "1" / "child.out").read_text()[-3000:]
    assert current["work_started_at"] is not None
    cells = conn.execute("SELECT cells FROM outbox ORDER BY id DESC LIMIT 1").fetchone()["cells"]
    assert cells["status"] == "fake_extractor: success"
    assert cells["title"] == "A fake title"

    # logs in the slot's folder, with the lines the timings use
    debug_log = (tmp_path / "slots" / "1" / "logs" / "1debug.log").read_text()
    assert "STARTED" in debug_log
    assert "DONE https://example.com/post/1" in debug_log
    assert (tmp_path / "slots" / "1" / "logs" / "hangs.log").exists()
    assert not (tmp_path / "logs").exists(), "nothing in the shared folder"

    # the job's folder reached the child, and files were worked on under the slot's temp folder
    stored = list((tmp_path / "stored").rglob("*.txt"))
    assert len(stored) == 1
    assert str(tmp_path / "slots" / "1" / "tmp") in stored[0].read_text()
    assert "for folder a-folder" in stored[0].read_text()
    assert not (tmp_path / "slots" / "1" / "tmp").exists(), "wiped afterwards"


def test_real_child_with_a_failing_row_reports_it(conn, run_for_real):
    _, (job_id,) = run_for_real(["https://example.com/fail"])
    # the extractor's error is caught by the orchestrator, so the row ends as nothing archived, as under cron
    assert db.get_job(conn, job_id)["status"] == "done"
    cells = conn.execute("SELECT cells FROM outbox ORDER BY id DESC LIMIT 1").fetchone()["cells"]
    assert "success" not in cells["status"]


def test_real_child_with_a_bad_config_is_a_setup_failure(conn, run_for_real, tmp_path):
    bad_config = ORCHESTRATION.format(save_to=tmp_path / "stored").replace("fake_extractor", "no_such_extractor")
    _, (job_id,) = run_for_real(["https://example.com/post/1"], config=bad_config)
    current = db.get_job(conn, job_id)
    assert (current["status"], current["attempts"], current["work_started_at"]) == ("queued", 0, None)
    assert conn.execute("SELECT setup_failures FROM sheets").fetchone()["setup_failures"] == 1
    assert (tmp_path / "slots" / "1" / "child.out").read_text() != ""


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
    assert current["status"] == "failed", (tmp_path / "slots" / "1" / "child.out").read_text()[-3000:]
    assert "killed" in current["error"]
    assert not (tmp_path / "slots" / "1" / "tmp").exists()
