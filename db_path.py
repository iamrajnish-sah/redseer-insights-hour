"""
Resolve a durable SQLite path.

Priority:
  1. DATABASE_PATH
  2. PERSISTENT_DISK_PATH / RENDER_DISK_PATH (Render disk)
  3. RAILWAY_VOLUME_MOUNT_PATH (Railway volume)
  4. /tmp on Vercel (ephemeral — needs Blob backup)
  5. ./news.db locally
"""

import os


def resolve_database_path():
    explicit = (os.environ.get("DATABASE_PATH") or "").strip()
    if explicit:
        parent = os.path.dirname(explicit)
        if parent:
            os.makedirs(parent, exist_ok=True)
        return explicit

    disk = (
        os.environ.get("PERSISTENT_DISK_PATH")
        or os.environ.get("RENDER_DISK_PATH")
        or ""
    ).strip()
    if disk:
        os.makedirs(disk, exist_ok=True)
        return os.path.join(disk, "news.db")

    railway = (os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()
    if railway:
        os.makedirs(railway, exist_ok=True)
        return os.path.join(railway, "news.db")

    if os.environ.get("VERCEL"):
        return "/tmp/news.db"

    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "news.db")


def using_persistent_disk():
    if (os.environ.get("DATABASE_PATH") or "").strip():
        path = os.environ["DATABASE_PATH"].strip()
        return not path.startswith("/tmp")
    return bool(
        (os.environ.get("PERSISTENT_DISK_PATH") or "").strip()
        or (os.environ.get("RENDER_DISK_PATH") or "").strip()
        or (os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()
    )
