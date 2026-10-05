"""
Database connection backend.

Prefer Turso (remote libSQL) when TURSO_DATABASE_URL + TURSO_AUTH_TOKEN are set.
Falls back to local / disk SQLite otherwise.

Why Turso: Vercel Hobby Blob free limits already wiped this project's history.
Turso keeps a real always-on SQLite-compatible DB (~5GB free) that survives
cold starts — no need to upload the whole news.db after every write.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager


def turso_configured():
    url = (os.environ.get("TURSO_DATABASE_URL") or os.environ.get("LIBSQL_URL") or "").strip()
    token = (os.environ.get("TURSO_AUTH_TOKEN") or os.environ.get("LIBSQL_AUTH_TOKEN") or "").strip()
    return bool(url and token)


def turso_url():
    return (os.environ.get("TURSO_DATABASE_URL") or os.environ.get("LIBSQL_URL") or "").strip()


def turso_token():
    return (os.environ.get("TURSO_AUTH_TOKEN") or os.environ.get("LIBSQL_AUTH_TOKEN") or "").strip()


class Row(dict):
    """sqlite3.Row-like mapping so existing row['col'] / dict(row) keep working."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)

    def keys(self):
        return list(super().keys())


class CompatibleCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def _as_row(self, row):
        if row is None:
            return None
        if isinstance(row, dict):
            return Row(row)
        cols = [d[0] for d in (self._cursor.description or [])]
        if not cols:
            return row
        return Row({cols[i]: row[i] for i in range(min(len(cols), len(row)))})

    def execute(self, sql, params=()):
        self._cursor.execute(sql, params or ())
        return self

    def executemany(self, sql, seq):
        self._cursor.executemany(sql, seq)
        return self

    def fetchone(self):
        return self._as_row(self._cursor.fetchone())

    def fetchall(self):
        return [self._as_row(r) for r in self._cursor.fetchall()]

    def fetchmany(self, size=None):
        rows = self._cursor.fetchmany(size) if size is not None else self._cursor.fetchmany()
        return [self._as_row(r) for r in rows]

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def description(self):
        return self._cursor.description

    def close(self):
        return self._cursor.close()

    def __iter__(self):
        for row in self._cursor:
            yield self._as_row(row)


class CompatibleConnection:
    def __init__(self, conn, backend="sqlite"):
        self._conn = conn
        self.backend = backend

    def execute(self, sql, params=()):
        return CompatibleCursor(self._conn.execute(sql, params or ()))

    def executemany(self, sql, seq):
        return CompatibleCursor(self._conn.executemany(sql, seq))

    def executescript(self, script):
        return self._conn.executescript(script)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()

    def cursor(self):
        return CompatibleCursor(self._conn.cursor())


def connect(db_path=None):
    """Open a connection to Turso (preferred) or local SQLite."""
    if turso_configured():
        import libsql

        conn = libsql.connect(
            database=turso_url(),
            auth_token=turso_token(),
        )
        return CompatibleConnection(conn, backend="turso")

    path = db_path or os.environ.get("DATABASE_PATH") or "news.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return CompatibleConnection(conn, backend="sqlite")


@contextmanager
def connection(db_path=None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def is_integrity_error(exc):
    if isinstance(exc, sqlite3.IntegrityError):
        return True
    text = str(exc).lower()
    return "unique constraint" in text or "constraint failed" in text


def is_duplicate_column_error(exc):
    if isinstance(exc, sqlite3.OperationalError):
        return True
    text = str(exc).lower()
    return "duplicate column" in text or "already exists" in text
