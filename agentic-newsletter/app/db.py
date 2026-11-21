"""Tiny portable DB layer: SQLite (local tests) and PostgreSQL via psycopg 3 (Docker).

SQL is written with '?' placeholders and translated for PostgreSQL. Never put a literal '?' or
'%' inside SQL text; pass such characters as parameters.
"""
from __future__ import annotations

import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

SQL_DIR = Path(__file__).resolve().parent.parent / "migrations" / "sql"


def new_id() -> str:
    return uuid.uuid4().hex


class Conn:
    def __init__(self, raw: Any, kind: str):
        self.raw = raw
        self.kind = kind  # "sqlite" | "pg"
        self._in_tx = False

    # -- internals ---------------------------------------------------
    def _q(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.kind == "pg" else sql

    def _ensure_tx(self) -> None:
        if self.kind == "sqlite" and not self._in_tx:
            self.raw.execute("BEGIN")
            self._in_tx = True

    def _raw_exec(self, sql: str, params: Sequence = ()):
        self._ensure_tx()
        return self.raw.execute(self._q(sql), tuple(params))

    # -- public API --------------------------------------------------
    def run(self, sql: str, params: Sequence = ()) -> int:
        """Execute a write/DDL statement, return affected row count."""
        cur = self._raw_exec(sql, params)
        return cur.rowcount

    def all(self, sql: str, params: Sequence = ()) -> list[dict]:
        cur = self._raw_exec(sql, params)
        rows = cur.fetchall()
        return [dict(r) for r in rows]

    def one(self, sql: str, params: Sequence = ()) -> dict | None:
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence = ()) -> Any:
        row = self.one(sql, params)
        return None if row is None else next(iter(row.values()))

    def commit(self) -> None:
        if self.kind == "sqlite":
            if self._in_tx:
                self.raw.execute("COMMIT")
                self._in_tx = False
        else:
            self.raw.commit()

    def rollback(self) -> None:
        if self.kind == "sqlite":
            if self._in_tx:
                self.raw.execute("ROLLBACK")
                self._in_tx = False
        else:
            self.raw.rollback()

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        name = "sp_" + uuid.uuid4().hex[:10]
        self._raw_exec(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            self._raw_exec(f"ROLLBACK TO SAVEPOINT {name}")
            self._raw_exec(f"RELEASE SAVEPOINT {name}")
            raise
        else:
            self._raw_exec(f"RELEASE SAVEPOINT {name}")

    def close(self) -> None:
        try:
            self.rollback()
        finally:
            self.raw.close()

    def __enter__(self) -> "Conn":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class Database:
    def __init__(self, url: str):
        self.url = url
        if url.startswith("sqlite:///"):
            self.kind = "sqlite"
            self.path = url[len("sqlite:///"):]
        elif url.startswith(("postgresql://", "postgres://")):
            self.kind = "pg"
        else:
            raise ValueError("DATABASE_URL must start with postgresql:// (or sqlite:/// for local tests)")

    def connect(self, retries: int = 1, delay: float = 2.0) -> Conn:
        last: Exception | None = None
        for attempt in range(max(1, retries)):
            try:
                if self.kind == "sqlite":
                    raw = sqlite3.connect(self.path, isolation_level=None, timeout=30)
                    raw.row_factory = sqlite3.Row
                    raw.execute("PRAGMA foreign_keys=ON")
                    return Conn(raw, "sqlite")
                import psycopg  # imported lazily: only needed for PostgreSQL
                from psycopg.rows import dict_row
                raw = psycopg.connect(self.url, row_factory=dict_row, connect_timeout=10)
                return Conn(raw, "pg")
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt + 1 < retries:
                    time.sleep(delay)
        raise ConnectionError(f"cannot connect to the database: {type(last).__name__}: {last}") from last


def split_sql(text: str) -> list[str]:
    text = re.sub(r"--[^\n]*", "", text)
    return [s.strip() for s in text.split(";") if s.strip()]


def apply_migrations_sql(conn: Conn) -> None:
    """Apply every migrations/sql/*.sql file (used by tests; Alembic uses the same files)."""
    for path in sorted(SQL_DIR.glob("*.sql")):
        for stmt in split_sql(path.read_text(encoding="utf-8")):
            conn.run(stmt)
    conn.commit()
