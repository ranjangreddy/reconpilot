"""Ledger backend abstraction: SQLite by default (zero setup), Postgres when
DATABASE_URL is set (see docker-compose.yml). SQL is kept portable; the only
dialect differences are placeholders (? vs %s) and autoincrement DDL, both
handled here so the rest of the codebase is backend-agnostic."""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Iterable


class Backend:
    name = "base"

    def execute(self, sql: str, params: Iterable[Any] = ()) -> Any:
        raise NotImplementedError

    def executemany(self, sql: str, seq: Iterable[Iterable[Any]]) -> Any:
        raise NotImplementedError

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None: ...

    def insert_ignore(self, table: str, columns: list[str], conflict: str) -> str:
        """INSERT that silently skips on primary-key conflict, both dialects."""
        cols = ", ".join(columns)
        if self.name == "postgres":
            ph = ", ".join(["%s"] * len(columns))
            return (
                f"INSERT INTO {table} ({cols}) VALUES ({ph}) "
                f"ON CONFLICT ({conflict}) DO NOTHING"
            )
        ph = ", ".join(["?"] * len(columns))
        return f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({ph})"


class SQLiteBackend(Backend):
    name = "sqlite"

    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        # WAL + NORMAL: fewer fsyncs per commit. The ledger's durability
        # comes from idempotency keys (re-ingest is always safe), not from
        # fsync-on-every-commit; this keeps file-backed runs usable on
        # slow / copy-on-write filesystems.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")

    def execute(self, sql: str, params: Iterable[Any] = ()):
        cur = self.conn.execute(sql, tuple(params))
        return cur

    def executemany(self, sql: str, seq: Iterable[Iterable[Any]]):
        return self.conn.executemany(sql, [tuple(p) for p in seq])

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


class PostgresBackend(Backend):
    name = "postgres"

    def __init__(self, url: str):
        import psycopg

        self.conn = psycopg.connect(url)
        self._psycopg = psycopg

    def execute(self, sql: str, params: Iterable[Any] = ()):
        cur = self.conn.execute(sql.replace("?", "%s"), tuple(params))
        return cur

    def executemany(self, sql: str, seq: Iterable[Iterable[Any]]):
        cur = self.conn.cursor()
        cur.executemany(sql.replace("?", "%s"), [tuple(p) for p in seq])
        return cur

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def connect() -> Backend:
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgres"):
        return PostgresBackend(url)
    path = os.environ.get("RECONPILOT_DB", "data/reconpilot.db")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    return SQLiteBackend(path)


def autoincrement(backend: Backend) -> str:
    return "SERIAL PRIMARY KEY" if backend.name == "postgres" else (
        "INTEGER PRIMARY KEY AUTOINCREMENT"
    )
