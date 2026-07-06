"""
db_persist.py

Vercel serverless /tmp is wiped between cold starts. When BLOB_READ_WRITE_TOKEN
is set (add Vercel Blob store in project settings), the SQLite file is restored
on startup and saved after each refresh.
"""

import os
import requests

BLOB_API = "https://blob.vercel-storage.com"


def _token():
    return os.environ.get("BLOB_READ_WRITE_TOKEN")


def _pathname():
    return os.environ.get("DATABASE_BLOB_PATH", "redseer-insight-hour/news.db")


def enabled():
    return bool(_token())


def restore_db(local_path):
    if not enabled():
        return False
    url = f"{BLOB_API}/{_pathname()}"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {_token()}"},
            timeout=30,
        )
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        directory = os.path.dirname(local_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(local_path, "wb") as handle:
            handle.write(resp.content)
        print(f"  [info] restored database from blob ({len(resp.content)} bytes)")
        return True
    except Exception as exc:
        print(f"  [warning] blob restore failed: {exc}")
        return False


def save_db(local_path):
    if not enabled() or not os.path.isfile(local_path):
        return False
    url = f"{BLOB_API}/{_pathname()}"
    try:
        with open(local_path, "rb") as handle:
            resp = requests.put(
                url,
                headers={
                    "Authorization": f"Bearer {_token()}",
                    "Content-Type": "application/octet-stream",
                    "x-add-random-suffix": "false",
                },
                data=handle.read(),
                timeout=60,
            )
        resp.raise_for_status()
        print("  [info] saved database to blob")
        return True
    except Exception as exc:
        print(f"  [warning] blob save failed: {exc}")
        return False
