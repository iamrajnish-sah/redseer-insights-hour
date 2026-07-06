"""
db_persist.py

Vercel serverless /tmp is wiped between cold starts. When BLOB_READ_WRITE_TOKEN
is set (Storage → Blob linked to your Vercel project), the SQLite database file
is restored on startup and saved after every write.

This keeps RSS/GNews/NewsAPI articles AND processed Mint/newspaper uploads safe.
"""

import os
import sqlite3
import requests

BLOB_API = "https://blob.vercel-storage.com"
_CACHED_BLOB_URL = None


def _token():
    return os.environ.get("BLOB_READ_WRITE_TOKEN")


def _pathname():
    return os.environ.get("DATABASE_BLOB_PATH", "redseer-insight-hour/news.db")


def enabled():
    return bool(_token())


def _headers(extra=None):
    headers = {
        "Authorization": f"Bearer {_token()}",
        "x-api-version": "7",
    }
    if extra:
        headers.update(extra)
    return headers


def _checkpoint_sqlite(local_path):
    """Flush SQLite WAL to the main file before upload."""
    if not os.path.isfile(local_path):
        return
    try:
        conn = sqlite3.connect(local_path)
        conn.execute("PRAGMA wal_checkpoint(FULL)")
        conn.close()
    except sqlite3.Error as exc:
        print(f"  [warning] sqlite checkpoint failed: {exc}")


def _list_blob_url():
    global _CACHED_BLOB_URL
    if _CACHED_BLOB_URL:
        return _CACHED_BLOB_URL
    try:
        resp = requests.get(
            BLOB_API,
            params={"prefix": _pathname()},
            headers=_headers(),
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"  [warning] blob list failed: {resp.status_code} {resp.text[:200]}")
            return None
        blobs = (resp.json() or {}).get("blobs") or []
        for blob in blobs:
            if blob.get("pathname") == _pathname() and blob.get("url"):
                _CACHED_BLOB_URL = blob["url"]
                return _CACHED_BLOB_URL
        if blobs and blobs[0].get("url"):
            _CACHED_BLOB_URL = blobs[0]["url"]
            return _CACHED_BLOB_URL
    except Exception as exc:
        print(f"  [warning] blob list error: {exc}")
    return None


def storage_status(local_path):
    size = os.path.getsize(local_path) if os.path.isfile(local_path) else 0
    return {
        "blob_configured": enabled(),
        "blob_pathname": _pathname(),
        "blob_url_cached": bool(_CACHED_BLOB_URL),
        "local_db_path": local_path,
        "local_db_bytes": size,
    }


def restore_db(local_path):
    global _CACHED_BLOB_URL
    if not enabled():
        print("  [info] BLOB_READ_WRITE_TOKEN not set — database will not persist on Vercel")
        return False

    blob_url = _list_blob_url()
    if not blob_url:
        print("  [info] no database blob found yet — starting fresh")
        return False

    try:
        resp = requests.get(blob_url, headers=_headers(), timeout=60)
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        directory = os.path.dirname(local_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(local_path, "wb") as handle:
            handle.write(resp.content)
        _CACHED_BLOB_URL = blob_url
        print(f"  [info] restored database from blob ({len(resp.content):,} bytes)")
        return True
    except Exception as exc:
        print(f"  [warning] blob restore failed: {exc}")
        return False


def save_db(local_path):
    global _CACHED_BLOB_URL
    if not enabled():
        return False
    if not os.path.isfile(local_path):
        return False

    _checkpoint_sqlite(local_path)

    try:
        with open(local_path, "rb") as handle:
            data = handle.read()
        resp = requests.put(
            f"{BLOB_API}/{_pathname()}",
            headers=_headers(
                {
                    "Content-Type": "application/x-sqlite3",
                    "x-add-random-suffix": "false",
                    "x-allow-overwrite": "true",
                }
            ),
            data=data,
            timeout=120,
        )
        if resp.status_code not in (200, 201):
            print(f"  [warning] blob save failed: {resp.status_code} {resp.text[:240]}")
            return False
        payload = resp.json() if resp.text else {}
        if payload.get("url"):
            _CACHED_BLOB_URL = payload["url"]
        print(f"  [info] saved database to blob ({len(data):,} bytes)")
        return True
    except Exception as exc:
        print(f"  [warning] blob save failed: {exc}")
        return False
