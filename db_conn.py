"""
Database connection backend.

Prefer Turso (remote libSQL) when TURSO_DATABASE_URL + TURSO_AUTH_TOKEN are set.
Falls back to local / disk SQLite otherwise.

Why Turso: Vercel Hobby Blob free limits already wiped this project's history.
Turso keeps a real always-on SQLite-compatible DB (~5GB free) that survives
cold starts — no need to upload the whole news.db after every write.

Vercel note: libsql:// defaults to WebSockets (wss), which often crashes serverless
functions. We connect over HTTPS/HTTP first via libsql_client.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

# Last connect/init error — exposed by /api/health so we don't brick the site.
LAST_ERROR = None
# Which driver succeeded: http / native / sqlite
LAST_DRIVER = None


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


def turso_http_url(url=None):
    """Convert libsql://host to https://host for Vercel-safe HTTP access."""
    text = _clean_env(url if url is not None else turso_url())
    if text.startswith("libsql://"):
        return "https://" + text[len("libsql://") :]
    return text


class Row(dict):
    """sqlite3.Row-like mapping so existing row['col'] / dict(row) keep working."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)

    def keys(self):
        return list(super().keys())


def _split_sql_script(script):
    """Split a SQL script into statements without breaking on ; inside strings."""
    statements = []
    buf = []
    in_single = False
    in_double = False
    i = 0
    text = script or ""
    while i < len(text):
        ch = text[i]
        if ch == "'" and not in_double:
            in_single = not in_single
            buf.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            buf.append(ch)
        elif ch == ";" and not in_single and not in_double:
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    stmt = "".join(buf).strip()
    if stmt:
        statements.append(stmt)
    return statements


def _normalize_params(params):
    if params is None:
        return None
    if isinstance(params, dict):
        return params
    if isinstance(params, (list, tuple)):
        return list(params) if params else None
    return [params]


class HttpLibsqlCursor:
    """Cursor adapter over libsql_client ResultSet."""

    def __init__(self, client, result=None):
        self._client = client
        self._result = result
        self._rows = list(result.rows) if result is not None else []
        self._index = 0
        self.lastrowid = getattr(result, "last_insert_rowid", None) if result is not None else None
        self.rowcount = getattr(result, "rows_affected", -1) if result is not None else -1
        cols = getattr(result, "columns", None) if result is not None else None
        self.description = [(c, None, None, None, None, None, None) for c in cols] if cols else None

    def _as_row(self, row):
        if row is None:
            return None
        if hasattr(row, "asdict"):
            return Row(row.asdict())
        if isinstance(row, dict):
            return Row(row)
        return row

    def execute(self, sql, params=()):
        try:
            result = self._client.execute(sql, _normalize_params(params))
        except KeyError as exc:
            # libsql_client raises KeyError('result') on odd HTTP payloads
            raise RuntimeError(
                f"Turso HTTP response missing result for SQL: {str(sql)[:120]}"
            ) from exc
        self._result = result
        self._rows = list(result.rows)
        self._index = 0
        self.lastrowid = result.last_insert_rowid
        self.rowcount = result.rows_affected
        self.description = [(c, None, None, None, None, None, None) for c in result.columns]
        return self

    def executemany(self, sql, seq):
        count = 0
        last = None
        for params in seq:
            try:
                last = self._client.execute(sql, _normalize_params(params))
            except KeyError as exc:
                raise RuntimeError(
                    f"Turso HTTP response missing result for SQL: {str(sql)[:120]}"
                ) from exc
            count += 1
        self._result = last
        self._rows = []
        self._index = 0
        self.lastrowid = getattr(last, "last_insert_rowid", None) if last is not None else None
        self.rowcount = count
        self.description = None
        return self

    def fetchone(self):
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return self._as_row(row)

    def fetchall(self):
        rows = self._rows[self._index :]
        self._index = len(self._rows)
        return [self._as_row(r) for r in rows]

    def fetchmany(self, size=None):
        if size is None:
            size = 1
        rows = self._rows[self._index : self._index + size]
        self._index += len(rows)
        return [self._as_row(r) for r in rows]

    def close(self):
        return None

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                break
            yield row


class HttpLibsqlConnection:
    """sqlite3-like wrapper around libsql_client HTTP ClientSync."""

    def __init__(self, client):
        self._client = client
        self.backend = "turso"

    def execute(self, sql, params=()):
        cur = HttpLibsqlCursor(self._client)
        return cur.execute(sql, params)

    def executemany(self, sql, seq):
        cur = HttpLibsqlCursor(self._client)
        return cur.executemany(sql, seq)

    def executescript(self, script):
        for stmt in _split_sql_script(script):
            try:
                self._client.execute(stmt)
            except KeyError as exc:
                raise RuntimeError(
                    f"Turso HTTP response missing result for SQL: {stmt[:120]}"
                ) from exc
            except Exception as exc:
                text = str(exc).lower()
                # CREATE IF NOT EXISTS / duplicate objects should not brick startup
                if (
                    "already exists" in text
                    or "duplicate column" in text
                    or "duplicate" in text
                ):
                    continue
                raise
        return self

    def commit(self):
        # HTTP statements auto-commit.
        return None

    def rollback(self):
        return None

    def close(self):
        try:
            self._client.close()
        except Exception:
            pass

    def cursor(self):
        return HttpLibsqlCursor(self._client)


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
        if hasattr(self._conn, "executescript"):
            return self._conn.executescript(script)
        for stmt in _split_sql_script(script):
            self._conn.execute(stmt)
        return None

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()

    def cursor(self):
        return CompatibleCursor(self._conn.cursor())


def _connect_turso_http(url, token):
    """Prefer HTTPS HTTP client — works on Vercel serverless."""
    from libsql_client import create_client_sync

    http_url = turso_http_url(url)
    client = create_client_sync(url=http_url, auth_token=token)
    conn = HttpLibsqlConnection(client)
    row = conn.execute("SELECT 1 AS ok").fetchone()
    if not row or row["ok"] != 1:
        raise RuntimeError("Turso HTTP ping failed")
    return conn


def _connect_turso_native(url, token):
    """Fallback: native libsql binding.

    Prefer HTTPS URL so Vercel does not open WebSockets.
    """
    import libsql

    candidates = []
    http_url = turso_http_url(url)
    if http_url.startswith("https://"):
        candidates.append(http_url)
    if url not in candidates:
        candidates.append(url)

    last_exc = None
    for candidate in candidates:
        try:
            conn = libsql.connect(database=candidate, auth_token=token)
            CompatibleCursor(conn.execute("SELECT 1 AS ok")).fetchone()
            return CompatibleConnection(conn, backend="turso")
        except Exception as exc:
            last_exc = exc
            print(f"[db] native libsql failed for {candidate[:48]}: {exc}")
    raise RuntimeError(str(last_exc) if last_exc else "native libsql connect failed")


def _connect_turso_embedded(url, token, db_path):
    """Fallback: local replica in /tmp synced to Turso primary."""
    import libsql

    local_path = os.environ.get("TURSO_REPLICA_PATH") or os.path.join(
        "/tmp", "turso-replica.db"
    )
    conn = libsql.connect(local_path, sync_url=url, auth_token=token)
    if hasattr(conn, "sync"):
        conn.sync()
    CompatibleCursor(conn.execute("SELECT 1 AS ok")).fetchone()
    return CompatibleConnection(conn, backend="turso")


def connect(db_path=None):
    """Open a connection to Turso (preferred) or local SQLite."""
    global LAST_ERROR, LAST_DRIVER
    problem = turso_config_problem()
    if turso_url() or turso_token():
        if problem and not turso_configured():
            LAST_ERROR = problem
            LAST_DRIVER = None
            # Fall through to local so the website still boots
            print(f"[db] Turso env invalid — {problem}")
        elif turso_configured():
            url = turso_url()
            token = turso_token()
            errors = []

            # 1) HTTPS HTTP — required for reliable Vercel serverless
            try:
                conn = _connect_turso_http(url, token)
                LAST_ERROR = None
                LAST_DRIVER = "http"
                print(f"[db] Turso connected via HTTPS ({turso_http_url(url)})")
                return conn
            except Exception as exc:
                errors.append(f"http: {exc}")
                print(f"[db] Turso HTTP connect failed: {exc}")

            # 2) Native remote URL (HTTPS first, then libsql://)
            try:
                conn = _connect_turso_native(url, token)
                LAST_ERROR = None
                LAST_DRIVER = "native"
                print("[db] Turso connected via native libsql")
                return conn
            except Exception as exc:
                errors.append(f"native: {exc}")
                print(f"[db] Turso native connect failed: {exc}")

            # 3) Embedded replica in /tmp
            try:
                conn = _connect_turso_embedded(url, token, db_path)
                LAST_ERROR = None
                LAST_DRIVER = "embedded"
                print("[db] Turso connected via embedded replica")
                return conn
            except Exception as exc:
                errors.append(f"embedded: {exc}")
                print(f"[db] Turso embedded connect failed: {exc}")

            LAST_ERROR = "Turso connect failed: " + " | ".join(errors)
            LAST_DRIVER = None
            print(f"[db] {LAST_ERROR} — falling back to local SQLite")

    path = db_path or os.environ.get("DATABASE_PATH") or "news.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    if not turso_configured():
        LAST_DRIVER = "sqlite"
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
