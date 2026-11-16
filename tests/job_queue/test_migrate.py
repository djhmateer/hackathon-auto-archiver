"""Migrations, in their own Postgres schema so the other tests' tables aren't involved"""

import psycopg
import pytest

from auto_archiver.job_queue import migrate


@pytest.fixture
def empty(conn):
    conn.execute("DROP SCHEMA IF EXISTS migrate_test CASCADE")
    conn.execute("CREATE SCHEMA migrate_test")
    conn.execute("SET search_path TO migrate_test")
    yield conn
    conn.execute("DROP SCHEMA migrate_test CASCADE")


def tables(conn) -> set[str]:
    rows = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'migrate_test'").fetchall()
    return {row["tablename"] for row in rows}


def test_applies_in_filename_order_and_records_them(empty, tmp_path):
    (tmp_path / "002_second.sql").write_text("ALTER TABLE a ADD COLUMN b int;")
    (tmp_path / "001_first.sql").write_text("CREATE TABLE a (id int);")
    assert migrate.migrate(empty, tmp_path) == ["001_first.sql", "002_second.sql"]
    assert tables(empty) == {"a", "schema_migrations"}


def test_running_again_only_applies_new_files(empty, tmp_path):
    (tmp_path / "001_first.sql").write_text("CREATE TABLE a (id int);")
    migrate.migrate(empty, tmp_path)
    assert migrate.migrate(empty, tmp_path) == []
    (tmp_path / "002_second.sql").write_text("CREATE TABLE b (id int);")
    assert migrate.migrate(empty, tmp_path) == ["002_second.sql"]


def test_a_failure_rolls_back_that_file_and_stops(empty, tmp_path):
    (tmp_path / "001_first.sql").write_text("CREATE TABLE a (id int);")
    (tmp_path / "002_bad.sql").write_text("CREATE TABLE half_done (id int); SELECT * FROM no_such_table;")
    (tmp_path / "003_later.sql").write_text("CREATE TABLE later (id int);")
    with pytest.raises(psycopg.errors.UndefinedTable):
        migrate.migrate(empty, tmp_path)
    assert tables(empty) == {"a", "schema_migrations"}
    recorded = empty.execute("SELECT filename FROM schema_migrations").fetchall()
    assert [row["filename"] for row in recorded] == ["001_first.sql"]


def test_the_real_migrations_build_the_schema(empty):
    assert "001_initial.sql" in migrate.migrate(empty)
    assert {"sheets", "jobs", "sheet_writes", "admin_alerts", "heartbeats"} <= tables(empty)


def test_014_fills_in_the_outcome_of_finished_jobs_from_their_status_text(empty, tmp_path):
    for path in sorted(migrate.MIGRATIONS_DIR.glob("*.sql")):
        if path.name < "014":
            (tmp_path / path.name).write_bytes(path.read_bytes())
    migrate.migrate(empty, tmp_path)
    sheet = empty.execute(
        "INSERT INTO sheets (spreadsheet_id, worksheet_gid, worksheet, config) VALUES ('s1', 1, 't1', 'c') RETURNING id"
    ).fetchone()["id"]
    cases = [
        ("done", "vimeo: success", "success"),
        ("done", "[cached] vimeo: success", "success"),
        ("done", "yt-dlp_Twitter: success (17 uploads failed - see logs)", "partial"),
        ("done", "wayback: success (blocked by site - HTTP 403)", "partial"),
        ("done", "no archiver (blocked by site - please requeue) (screenshot blocked by site)", "blocked"),
        ("done", "no archiver (aa IP blocked)", "blocked"),
        ("done", "no archiver (screenshot blocked by site)", "nothing_archived"),
        ("done", "wacz: success (site returned HTTP 404)", "nothing_archived"),
        ("done", "nothing archived", "nothing_archived"),
        ("failed", "Archive failed - a reason", "failed"),
        ("rejected", "Rejected - a reason", None),
    ]
    ids = []
    for row, (status, text, _) in enumerate(cases, start=2):
        job_id = empty.execute(
            "INSERT INTO jobs (sheet_id, row_number, url, domain, status) VALUES (%s, %s, 'u', 'd', %s) RETURNING id",
            (sheet, row, status),
        ).fetchone()["id"]
        # an older write first, so only the last one counts
        for cell in ("Archive in progress", text):
            empty.execute(
                "INSERT INTO sheet_writes (job_id, cells) VALUES (%s, jsonb_build_object('status', %s::text))",
                (job_id, cell),
            )
        ids.append(job_id)

    assert "014_job_outcome.sql" in migrate.migrate(empty)
    outcomes = [empty.execute("SELECT outcome FROM jobs WHERE id = %s", (i,)).fetchone()["outcome"] for i in ids]
    assert outcomes == [outcome for _, _, outcome in cases]


def test_check_on_a_new_database_says_everything_is_pending(empty, tmp_path):
    (tmp_path / "001_first.sql").write_text("CREATE TABLE a (id int);")
    assert migrate.check(empty, tmp_path) == (["001_first.sql"], [])


def test_check_passes_when_up_to_date(empty, tmp_path):
    (tmp_path / "001_first.sql").write_text("CREATE TABLE a (id int);")
    migrate.migrate(empty, tmp_path)
    assert migrate.check(empty, tmp_path) == ([], [])


def test_check_finds_a_new_file(empty, tmp_path):
    (tmp_path / "001_first.sql").write_text("CREATE TABLE a (id int);")
    migrate.migrate(empty, tmp_path)
    (tmp_path / "002_second.sql").write_text("CREATE TABLE b (id int);")
    assert migrate.check(empty, tmp_path) == (["002_second.sql"], [])


def test_check_finds_an_edited_file_and_migrate_refuses(empty, tmp_path):
    (tmp_path / "001_first.sql").write_text("CREATE TABLE a (id int);")
    migrate.migrate(empty, tmp_path)
    (tmp_path / "001_first.sql").write_text("CREATE TABLE a (id bigint);")
    (tmp_path / "002_second.sql").write_text("CREATE TABLE b (id int);")
    pending, problems = migrate.check(empty, tmp_path)
    assert pending == ["002_second.sql"]
    assert "001_first.sql has been edited" in problems[0]
    with pytest.raises(RuntimeError):
        migrate.migrate(empty, tmp_path)
    assert "b" not in tables(empty)


def test_check_finds_a_database_ahead_of_the_code(empty, tmp_path):
    (tmp_path / "001_first.sql").write_text("CREATE TABLE a (id int);")
    migrate.migrate(empty, tmp_path)
    (tmp_path / "001_first.sql").unlink()
    assert "this code doesn't have it" in migrate.check(empty, tmp_path)[1][0]
