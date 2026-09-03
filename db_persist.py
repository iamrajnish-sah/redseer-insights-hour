"""
db_persist.py

Vercel serverless /tmp is wiped between cold starts. When Blob is linked to the
project (BLOB_STORE_ID + OIDC or BLOB_READ_WRITE_TOKEN), the SQLite file is
restored on startup and saved after every write.

Vercel now connects Blob via OIDC — use VERCEL_OIDC_TOKEN on deployed functions.
"""

import os
import sqlite3
import tempfile
import time
import urllib.parse
import requests

BLOB_API = os.environ.get("VERCEL_BLOB_API_URL", "https://vercel.com/api/blob").rstrip("/")
BLOB_API_VERSION = os.environ.get("VERCEL_BLOB_API_VERSION", "12")
_CACHED_BLOB_URL = None
_LAST_SAVE_ERROR = None
_LAST_SAVE_OK = None
_LAST_AUTH_MODE = None
# Byte slack for size comparisons (SQLite page granularity / WAL churn).
_SIZE_MARGIN_BYTES = 16_384


def _read_write_token():
    return (os.environ.get("BLOB_READ_WRITE_TOKEN") or "").strip()


def _oidc_token():
    return (os.environ.get("VERCEL_OIDC_TOKEN") or "").strip()


def _pathname():
    return os.environ.get("DATABASE_BLOB_PATH", "redseer-insight-hour/news.db")


def _store_id():
    """Bare store id for x-vercel-blob-store-id (strip store_ prefix)."""
    store = (os.environ.get("BLOB_STORE_ID") or "").strip()
    if store:
        return store.removeprefix("store_")
    token = _read_write_token()
    if token:
        parts = token.split("_")
        if len(parts) >= 4 and parts[3]:
            return parts[3].removeprefix("store_")
    return None


def _auth():
    """Prefer OIDC on Vercel (required when Blob is OIDC-linked)."""
    global _LAST_AUTH_MODE
    store_id = _store_id()
    if not store_id:
        _LAST_AUTH_MODE = None
        return None, None

    oidc = _oidc_token()
    if oidc:
        _LAST_AUTH_MODE = "oidc"
        return oidc, store_id

    rw = _read_write_token()
    if rw:
        _LAST_AUTH_MODE = "read_write"
        return rw, store_id

    _LAST_AUTH_MODE = None
    return None, store_id


def enabled():
    token, store_id = _auth()
    return bool(token and store_id)


def _headers(extra=None):
    token, store_id = _auth()
    if not token:
        raise RuntimeError("No Blob credentials (need VERCEL_OIDC_TOKEN or BLOB_READ_WRITE_TOKEN plus BLOB_STORE_ID)")
    headers = {
        "Authorization": f"Bearer {token}",
        "x-api-version": BLOB_API_VERSION,
        "x-vercel-blob-store-id": store_id,
    }
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


def _save_with_vercel_sdk(local_path):
    try:
        from vercel.blob import BlobClient
    except ImportError:
        return None

    try:
        with open(local_path, "rb") as handle:
            data = handle.read()
        client = BlobClient()
        result = client.put(
            _pathname(),
            data,
            access="private",
            content_type="application/x-sqlite3",
            add_random_suffix=False,
            allow_overwrite=True,
        )
        global _CACHED_BLOB_URL
        _CACHED_BLOB_URL = getattr(result, "download_url", None) or getattr(result, "url", None)
        return len(data)
    except Exception as exc:
        print(f"  [warning] vercel SDK blob save failed: {exc}")
        return False


def _list_blob_url(retries=3):
    """Find the backup blob URL. Retries — a transient failure here must not
    make the app 'start fresh' and later clobber the cloud backup."""
    global _CACHED_BLOB_URL
    if _CACHED_BLOB_URL:
        return _CACHED_BLOB_URL
    if not enabled():
        return None
    for attempt in range(retries):
        try:
            resp = requests.get(
                _api_url({"prefix": _pathname()}),
                headers=_headers(),
                timeout=30,
            )
            if resp.status_code == 200:
                blobs = (resp.json() or {}).get("blobs") or []
                for blob in blobs:
                    if blob.get("pathname") == _pathname():
                        _CACHED_BLOB_URL = blob.get("downloadUrl") or blob.get("url")
                        if _CACHED_BLOB_URL:
                            return _CACHED_BLOB_URL
                if blobs:
                    _CACHED_BLOB_URL = blobs[0].get("downloadUrl") or blobs[0].get("url")
                    return _CACHED_BLOB_URL
                return None  # listing worked, no backup exists yet
            print(f"  [warning] blob list failed (attempt {attempt + 1}/{retries}): {resp.status_code} {resp.text[:240]}")
        except Exception as exc:
            print(f"  [warning] blob list error (attempt {attempt + 1}/{retries}): {exc}")
        if attempt < retries - 1:
            time.sleep(1.5 * (attempt + 1))
    return None


def storage_status(local_path):
    token, store_id = _auth()
    size = os.path.getsize(local_path) if os.path.isfile(local_path) else 0
    return {
        "blob_configured": enabled(),
        "blob_store_id": store_id,
        "blob_pathname": _pathname(),
        "blob_api": BLOB_API,
        "auth_mode": _LAST_AUTH_MODE or ("oidc" if _oidc_token() else "read_write" if _read_write_token() else None),
        "has_oidc_token": bool(_oidc_token()),
        "has_read_write_token": bool(_read_write_token()),
        "blob_url_cached": bool(_CACHED_BLOB_URL),
        "local_db_path": local_path,
        "local_db_bytes": size,
        "last_save_ok": _LAST_SAVE_OK,
        "last_save_error": _LAST_SAVE_ERROR,
    }


def _blob_remote_size(blob_url):
    if not blob_url or not enabled():
        return 0
    try:
        resp = requests.head(blob_url, headers=_headers(), timeout=20, allow_redirects=True)
        if resp.status_code == 404:
            return 0
        return int(resp.headers.get("content-length") or 0)
    except Exception:
        return 0


def _article_count(db_path):
    """Number of canonical (non-duplicate) articles in a SQLite file. -1 on error."""
    if not db_path or not os.path.isfile(db_path):
        return 0
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM articles WHERE duplicate_of IS NULL"
            ).fetchone()
            return row[0] if row else 0
        finally:
            conn.close()
    except sqlite3.Error:
        return -1


def _download_blob(blob_url):
    try:
        resp = requests.get(blob_url, headers=_headers(), timeout=60)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.content
    except Exception as exc:
        print(f"  [warning] blob download failed: {exc}")
        return None


def _write_temp_db(data):
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    try:
        handle.write(data)
    finally:
        handle.close()
    return handle.name


def _table_columns(conn, schema, table):
    try:
        rows = conn.execute(f"PRAGMA {schema}.table_info({table})").fetchall()
        return [row[1] for row in rows]
    except sqlite3.Error:
        return []


def _merge_remote_into_local(local_path, remote_bytes):
    """Union rows from a remote DB snapshot into the local DB.

    Articles are matched by URL, or by (title_key, pub_date) when no URL.
    NEVER deletes or overwrites local rows. Returns articles added."""
    remote_path = _write_temp_db(remote_bytes)
    added_articles = 0
    try:
        conn = sqlite3.connect(local_path)
        try:
            conn.execute("ATTACH DATABASE ? AS remote", (remote_path,))

            local_cols = _table_columns(conn, "main", "articles")
            remote_cols = _table_columns(conn, "remote", "articles")
            if local_cols and remote_cols:
                # id excluded (autoincrement); duplicate_of excluded (remote ids meaningless here)
                cols = [c for c in remote_cols if c in local_cols and c not in ("id", "duplicate_of")]
                col_list = ", ".join(cols)
                sel_list = ", ".join(f"r.{c}" for c in cols)
                cur = conn.execute(
                    f"""INSERT OR IGNORE INTO main.articles ({col_list})
                        SELECT {sel_list} FROM remote.articles r
                        WHERE r.duplicate_of IS NULL
                          AND NOT EXISTS (
                            SELECT 1 FROM main.articles m
                            WHERE r.url IS NOT NULL AND r.url != '' AND m.url = r.url
                          )
                          AND NOT EXISTS (
                            SELECT 1 FROM main.articles m2
                            WHERE r.title_key IS NOT NULL AND r.title_key != ''
                              AND m2.title_key = r.title_key AND m2.pub_date IS r.pub_date
                          )"""
                )
                added_articles = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

            if _table_columns(conn, "remote", "link_cache") and _table_columns(conn, "main", "link_cache"):
                conn.execute(
                    """INSERT OR IGNORE INTO main.link_cache (source_url, resolved_url, image_url, updated_at)
                       SELECT source_url, resolved_url, image_url, updated_at FROM remote.link_cache"""
                )

            if _table_columns(conn, "remote", "subscribers") and _table_columns(conn, "main", "subscribers"):
                conn.execute(
                    """INSERT OR IGNORE INTO main.subscribers
                       (name, email, company, designation, status, token, created_at, updated_at)
                       SELECT r.name, r.email, r.company, r.designation, r.status, r.token, r.created_at, r.updated_at
                       FROM remote.subscribers r
                       WHERE NOT EXISTS (
                         SELECT 1 FROM main.subscribers m WHERE m.email = r.email COLLATE NOCASE
                       )"""
                )
                conn.execute(
                    """INSERT OR IGNORE INTO main.subscriber_sectors (subscriber_id, sector, created_at)
                       SELECT m.id, rs.sector, rs.created_at
                       FROM remote.subscriber_sectors rs
                       JOIN remote.subscribers r ON r.id = rs.subscriber_id
                       JOIN main.subscribers m ON m.email = r.email COLLATE NOCASE"""
                )

            if _table_columns(conn, "remote", "intelligence_reports") and _table_columns(conn, "main", "intelligence_reports"):
                local_intel = _table_columns(conn, "main", "intelligence_reports")
                remote_intel = _table_columns(conn, "remote", "intelligence_reports")
                intel_cols = [c for c in remote_intel if c in local_intel and c != "id"]
                if intel_cols:
                    col_list = ", ".join(intel_cols)
                    sel_list = ", ".join(f"r.{c}" for c in intel_cols)
                    conn.execute(
                        f"""INSERT OR IGNORE INTO main.intelligence_reports ({col_list})
                            SELECT {sel_list} FROM remote.intelligence_reports r
                            WHERE NOT EXISTS (
                              SELECT 1 FROM main.intelligence_reports m
                              WHERE m.sector = r.sector
                                AND m.start_date = r.start_date
                                AND m.end_date = r.end_date
                            )"""
                    )

            conn.commit()
            conn.execute("DETACH DATABASE remote")
        finally:
            conn.close()
    finally:
        try:
            os.remove(remote_path)
        except OSError:
            pass
    return added_articles


def restore_db(local_path, force=False):
    global _CACHED_BLOB_URL
    if not enabled():
        print("  [info] Blob not configured — set BLOB_STORE_ID and BLOB_READ_WRITE_TOKEN on Vercel")
        return False

    blob_url = _list_blob_url()
    if not blob_url:
        print("  [info] no database blob found yet — starting fresh")
        return False

    local_size = os.path.getsize(local_path) if os.path.isfile(local_path) else 0
    local_count = _article_count(local_path)

    # Case 1: local DB empty or unreadable — download the full backup file.
    if local_size < 8_192 or local_count <= 0:
        data = _download_blob(blob_url)
        if not data:
            return False
        if len(data) < 512 and not force:
            print("  [info] cloud blob too small — skip restore")
            return False
        directory = os.path.dirname(local_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(local_path, "wb") as handle:
            handle.write(data)
        _CACHED_BLOB_URL = blob_url
        print(
            f"  [restore] downloaded cloud backup ({len(data):,} bytes, "
            f"{_article_count(local_path)} articles)"
        )
        return True

    # Case 2: local DB has articles — NEVER overwrite the file wholesale.
    # Always merge cloud rows (articles, subscribers, intelligence briefs).
    # Skipping this when sizes looked similar wiped older subscribers on save.
    data = _download_blob(blob_url)
    if not data:
        return False
    try:
        added = _merge_remote_into_local(local_path, data)
    except Exception as exc:
        print(f"  [warning] blob restore merge failed: {exc}")
        return False
    _CACHED_BLOB_URL = blob_url
    print(
        f"  [restore] merged cloud backup into local DB: +{added} article(s), "
        f"local now has {_article_count(local_path)} articles (nothing deleted)"
    )
    return True


def save_db(local_path, force=False):
    global _CACHED_BLOB_URL, _LAST_SAVE_ERROR, _LAST_SAVE_OK
    _LAST_SAVE_ERROR = None
    _LAST_SAVE_OK = False

    if not enabled():
        _LAST_SAVE_ERROR = "Blob not configured — add BLOB_STORE_ID + BLOB_READ_WRITE_TOKEN on Vercel, then redeploy"
        return False
    if not os.path.isfile(local_path):
        _LAST_SAVE_ERROR = "local database file missing"
        return False

    _checkpoint_sqlite(local_path)
    local_size = os.path.getsize(local_path)
    blob_url = _list_blob_url()
    if blob_url:
        # Always fold cloud subscribers/briefs/articles into local before upload
        # so a cold instance cannot overwrite older emails with a smaller list.
        data = _download_blob(blob_url)
        if data:
            try:
                added = _merge_remote_into_local(local_path, data)
                if added:
                    print(f"  [persist] merged {added} article(s) from cloud backup before save")
            except Exception as exc:
                print(f"  [warning] pre-save cloud merge failed: {exc}")
        _checkpoint_sqlite(local_path)
        local_size = os.path.getsize(local_path)
        if not force:
            remote_size = _blob_remote_size(blob_url)
            if remote_size > local_size * 1.02 + _SIZE_MARGIN_BYTES:
                local_count = _article_count(local_path)
                remote_count = -1
                if data:
                    remote_tmp = _write_temp_db(data)
                    try:
                        remote_count = _article_count(remote_tmp)
                    finally:
                        try:
                            os.remove(remote_tmp)
                        except OSError:
                            pass
                if remote_count > local_count:
                    _LAST_SAVE_ERROR = (
                        f"Refused to overwrite cloud backup ({remote_count} articles, {remote_size:,} B) "
                        f"with smaller local dataset ({local_count} articles, {local_size:,} B). "
                        f"Use Restore from Cloud, or force-save to override."
                    )
                    print(f"  [integrity] {_LAST_SAVE_ERROR}")
                    return False
                _checkpoint_sqlite(local_path)
                local_size = os.path.getsize(local_path)

    sdk_result = _save_with_vercel_sdk(local_path)
    if sdk_result and sdk_result is not False:
        _LAST_SAVE_OK = True
        print(f"  [info] saved database via Vercel SDK ({sdk_result:,} bytes)")
        return True

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
        print(f"  [info] saved database to blob ({len(data):,} bytes, auth={_LAST_AUTH_MODE})")
        return True
    except Exception as exc:
        _LAST_SAVE_ERROR = str(exc)
        print(f"  [warning] blob save failed: {exc}")
        return False
