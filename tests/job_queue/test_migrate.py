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
    assert {"sheets", "jobs", "outbox", "alerts", "heartbeats"} <= tables(empty)


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
