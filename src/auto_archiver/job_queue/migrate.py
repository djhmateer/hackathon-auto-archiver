"""
Applies the SQL files in migrations/ that this database hasn't had yet, in filename order.

Each file runs in its own transaction, so a failure leaves it unapplied, and stops the rest. To change the schema, add
the next numbered file. Never edit one that's been applied anywhere: its checksum is recorded, and the coordinator
won't start if it no longer matches.

    python -m auto_archiver.job_queue.migrate
"""

import hashlib
from pathlib import Path

import psycopg

from auto_archiver.job_queue import db
from auto_archiver.utils.custom_logger import logger

MIGRATIONS_DIR = Path(__file__).with_name("migrations")


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(conn: psycopg.Connection, folder: Path = MIGRATIONS_DIR) -> tuple[list[str], list[str]]:
    """
    Compares the database with the files. Returns (files not applied yet, problems). Pending files are fixed by running
    migrate. Problems need a person: an applied file was edited, or the database is ahead of this code
    """
    files = {path.name: checksum(path) for path in sorted(folder.glob("*.sql"))}
    if conn.execute("SELECT to_regclass('schema_migrations') AS name").fetchone()["name"] is None:
        return list(files), []
    applied = {r["filename"]: r["checksum"] for r in conn.execute("SELECT * FROM schema_migrations").fetchall()}
    problems = []
    for name, saved in sorted(applied.items()):
        if name not in files:
            problems.append(f"{name} is applied to this database, but this code doesn't have it")
        elif files[name] != saved:
            problems.append(f"{name} has been edited since it was applied. Put it back, and add a new file instead")
    return [name for name in files if name not in applied], problems


def migrate(conn: psycopg.Connection, folder: Path = MIGRATIONS_DIR) -> list[str]:
    """Returns the filenames it applied. Applies nothing if there are problems"""
    pending, problems = check(conn, folder)
    if problems:
        raise RuntimeError("; ".join(problems))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename text PRIMARY KEY,
            checksum text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    for name in pending:
        path = folder / name
        with conn.transaction():
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations (filename, checksum) VALUES (%s, %s)", (name, checksum(path)))
        logger.info(f"Applied {name}")
    return pending


def main() -> None:
    with db.connect() as conn:
        applied = migrate(conn)
    logger.info(f"{len(applied)} migration(s) applied, database is up to date")


if __name__ == "__main__":
    main()
