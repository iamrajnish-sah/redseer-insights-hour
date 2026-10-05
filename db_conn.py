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

# Last connect/init error — exposed by /api/health so we don't brick the site.
LAST_ERROR = None


def _clean_env(value):
    text = (value or "").strip()
    # Common paste mistakes from dashboards / chat examples
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1].strip()
    return text


def turso_url():
    return _clean_env(os.environ.get("TURSO_DATABASE_URL") or os.environ.get("LIBSQL_URL") or "")


def turso_token():
    return _clean_env(os.environ.get("TURSO_AUTH_TOKEN") or os.environ.get("LIBSQL_AUTH_TOKEN") or "")


def turso_configured():
    url = turso_url()
    token = turso_token()
    if not url or not token:
        return False
    # Reject the placeholder values from setup docs so we don't crash the site
    if "...." in url or "your_token_here" in token.lower() or token.lower() == "your-turso-token":
        return False
    return True


def turso_config_problem():
    """Human message if env looks wrong / incomplete."""
    url = turso_url()
    token = turso_token()
    if not url and not token:
        return "TURSO_DATABASE_URL and TURSO_AUTH_TOKEN are not set on Vercel."
    if not url:
        return "TURSO_DATABASE_URL is missing."
    if not token:
        return "TURSO_AUTH_TOKEN is missing."
    if "...." in url:
        return (
            "TURSO_DATABASE_URL still looks like a placeholder (contains ....). "
            "Paste the real libsql://…turso.io URL from the Turso dashboard."
        )
    if "your_token_here" in token.lower() or token.lower() == "your-turso-token":
        return (
            "TURSO_AUTH_TOKEN is still the placeholder text. "
            "In Turso → your DB → Tokens → Create Token, then paste the real token into Vercel."
        )
    if not url.startswith(("libsql://", "https://", "http://")):
        return f"TURSO_DATABASE_URL must start with libsql:// or https:// (got: {url[:32]}…)"
    return None


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


def _normalize_turso_database_arg(url):
    """libsql Python client accepts libsql:// or https://."""
    url = _clean_env(url)
    return url


def connect(db_path=None):
    """Open a connection to Turso (preferred) or local SQLite."""
    global LAST_ERROR
    problem = turso_config_problem()
    if turso_url() or turso_token():
        if problem and not turso_configured():
            LAST_ERROR = problem
            # Fall through to local so the website still boots
            print(f"[db] Turso env invalid — {problem}")
        elif turso_configured():
            try:
                import libsql

                url = _normalize_turso_database_arg(turso_url())
                token = turso_token()
                conn = libsql.connect(database=url, auth_token=token)
                # Prove the connection works before handing it to the app
                CompatibleCursor(conn.execute("SELECT 1 AS ok")).fetchone()
                LAST_ERROR = None
                return CompatibleConnection(conn, backend="turso")
            except Exception as exc:
                # Fall back to local SQLite so a bad token never bricks the website.
                LAST_ERROR = f"Turso connect failed: {exc}"
                print(f"[db] {LAST_ERROR} — falling back to local SQLite")

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
