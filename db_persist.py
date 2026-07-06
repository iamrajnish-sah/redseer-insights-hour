"""
db_persist.py

Vercel serverless /tmp is wiped between cold starts. When BLOB_READ_WRITE_TOKEN
is set (Storage → Blob linked to your Vercel project), the SQLite database file
is restored on startup and saved after every write.

Uses the same HTTP API as @vercel/blob (vercel.com/api/blob, API version 12).
"""

import os
import sqlite3
import urllib.parse
import requests

BLOB_API = os.environ.get("VERCEL_BLOB_API_URL", "https://vercel.com/api/blob").rstrip("/")
BLOB_API_VERSION = os.environ.get("VERCEL_BLOB_API_VERSION", "12")
_CACHED_BLOB_URL = None
_LAST_SAVE_ERROR = None
_LAST_SAVE_OK = None


def _token():
    return (os.environ.get("BLOB_READ_WRITE_TOKEN") or "").strip()


def _pathname():
    return os.environ.get("DATABASE_BLOB_PATH", "redseer-insight-hour/news.db")


def enabled():
    return bool(_token())


def _store_id():
    """Store id from BLOB_STORE_ID env (preferred) or parsed from the rw token."""
    store = (os.environ.get("BLOB_STORE_ID") or "").strip()
    if store:
        return store.removeprefix("store_")
    token = _token()
    if token:
        parts = token.split("_")
        if len(parts) >= 4 and parts[3]:
            return parts[3].removeprefix("store_")
    return None


def _headers(extra=None):
    token = _token()
    headers = {
        "Authorization": f"Bearer {token}",
        "x-api-version": BLOB_API_VERSION,
    }
    store_id = _store_id()
    if store_id:
        headers["x-vercel-blob-store-id"] = store_id
    if extra:
        headers.update(extra)
    return headers


def _checkpoint_sqlite(local_path):
    if not os.path.isfile(local_path):
        return
    try:
        conn = sqlite3.connect(local_path)
        conn.execute("PRAGMA wal_checkpoint(FULL)")
        conn.close()
    except sqlite3.Error as exc:
        print(f"  [warning] sqlite checkpoint failed: {exc}")


def _api_url(query_params):
    return f"{BLOB_API}/?{urllib.parse.urlencode(query_params)}"


def _list_blob_url():
    global _CACHED_BLOB_URL
    if _CACHED_BLOB_URL:
        return _CACHED_BLOB_URL
    try:
        resp = requests.get(
            _api_url({"prefix": _pathname()}),
            headers=_headers(),
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"  [warning] blob list failed: {resp.status_code} {resp.text[:240]}")
            return None
        blobs = (resp.json() or {}).get("blobs") or []
        for blob in blobs:
            if blob.get("pathname") == _pathname():
                _CACHED_BLOB_URL = blob.get("downloadUrl") or blob.get("url")
                if _CACHED_BLOB_URL:
                    return _CACHED_BLOB_URL
        if blobs:
            _CACHED_BLOB_URL = blobs[0].get("downloadUrl") or blobs[0].get("url")
            return _CACHED_BLOB_URL
    except Exception as exc:
        print(f"  [warning] blob list error: {exc}")
    return None


def storage_status(local_path):
    size = os.path.getsize(local_path) if os.path.isfile(local_path) else 0
    return {
        "blob_configured": enabled(),
        "blob_store_id": _store_id(),
        "blob_pathname": _pathname(),
        "blob_api": BLOB_API,
        "blob_url_cached": bool(_CACHED_BLOB_URL),
        "local_db_path": local_path,
        "local_db_bytes": size,
        "last_save_ok": _LAST_SAVE_OK,
        "last_save_error": _LAST_SAVE_ERROR,
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
    global _CACHED_BLOB_URL, _LAST_SAVE_ERROR, _LAST_SAVE_OK
    _LAST_SAVE_ERROR = None
    _LAST_SAVE_OK = False

    if not enabled():
        _LAST_SAVE_ERROR = "BLOB_READ_WRITE_TOKEN not set"
        return False
    if not os.path.isfile(local_path):
        _LAST_SAVE_ERROR = "local database file missing"
        return False

    _checkpoint_sqlite(local_path)

    try:
        with open(local_path, "rb") as handle:
            data = handle.read()

        resp = requests.put(
            _api_url({"pathname": _pathname()}),
            headers=_headers(
                {
                    "x-vercel-blob-access": "private",
                    "x-add-random-suffix": "0",
                    "x-allow-overwrite": "1",
                    "x-content-type": "application/x-sqlite3",
                    "x-content-length": str(len(data)),
                }
            ),
            data=data,
            timeout=120,
        )

        if resp.status_code not in (200, 201):
            _LAST_SAVE_ERROR = f"HTTP {resp.status_code}: {resp.text[:300]}"
            print(f"  [warning] blob save failed: {_LAST_SAVE_ERROR}")
            return False

        payload = resp.json() if resp.text else {}
        _CACHED_BLOB_URL = payload.get("downloadUrl") or payload.get("url") or _CACHED_BLOB_URL
        _LAST_SAVE_OK = True
        print(f"  [info] saved database to blob ({len(data):,} bytes)")
        return True
    except Exception as exc:
        _LAST_SAVE_ERROR = str(exc)
        print(f"  [warning] blob save failed: {exc}")
        return False
