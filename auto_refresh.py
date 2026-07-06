"""
auto_refresh.py

Keeps the dashboard up to date without manual button clicks:
  - Runs on a schedule (Vercel Cron)
  - Runs when someone opens the site and news is empty or stale
"""

import os
from datetime import datetime, timedelta

import database
import dedupe
import rss_ingest
import gnews_ingest
import newsapi_ingest
from db_persist import save_db

META_LAST_REFRESH = "last_auto_refresh"
META_REFRESH_LOCK = "refresh_lock_until"


def _max_age_hours():
    return float(os.environ.get("AUTO_REFRESH_HOURS", "4"))


def _lock_minutes():
    return int(os.environ.get("AUTO_REFRESH_LOCK_MINUTES", "15"))


def get_last_refresh():
    raw = database.get_meta(META_LAST_REFRESH)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def should_refresh(force=False):
    if force:
        return True
    if database.get_relevant_count() == 0:
        return True
    last = get_last_refresh()
    if not last:
        return True
    return datetime.now() - last >= timedelta(hours=_max_age_hours())


def refresh_status():
    last = get_last_refresh()
    return {
        "stale": should_refresh(force=False),
        "last_refresh": last.isoformat(timespec="seconds") if last else None,
        "article_count": database.get_relevant_count(),
        "refresh_hours": _max_age_hours(),
    }


def _persist():
    from db_persist import save_db, enabled

    if not enabled():
        return False
    return save_db(database.DB_PATH)


def run_refresh(include_rss=True, include_gnews=True, include_newsapi=None):
    """Pull RSS + GNews (+ NewsAPI when configured) and dedupe."""
    if include_newsapi is None:
        include_newsapi = bool(os.environ.get("NEWSAPIKEY") or os.environ.get("NEWSAPI_KEY"))

    summary = {
        "rss": None,
        "gnews": None,
        "newsapi": None,
        "total_inserted": 0,
        "total_refreshed": 0,
    }

    if include_rss:
        articles, stats = rss_ingest.fetch_all()
        inserted, _, refreshed = database.insert_articles(articles)
        summary["rss"] = {"inserted": inserted, "refreshed": len(refreshed), **stats}
        summary["total_inserted"] += inserted
        summary["total_refreshed"] += len(refreshed)

    gnews_key = os.environ.get("GNEWSAPIKEY") or os.environ.get("GNEWS_API_KEY")
    if include_gnews and gnews_key:
        articles, stats = gnews_ingest.fetch_all()
        inserted, _, refreshed = database.insert_articles(articles)
        summary["gnews"] = {"inserted": inserted, "refreshed": len(refreshed), **stats}
        summary["total_inserted"] += inserted
        summary["total_refreshed"] += len(refreshed)

    if include_newsapi and (os.environ.get("NEWSAPIKEY") or os.environ.get("NEWSAPI_KEY")):
        articles, stats = newsapi_ingest.fetch_all()
        inserted, _, refreshed = database.insert_articles(articles)
        summary["newsapi"] = {"inserted": inserted, "refreshed": len(refreshed), **stats}
        summary["total_inserted"] += inserted
        summary["total_refreshed"] += len(refreshed)

    dedupe_stats = dedupe.run_all_dedupes()
    summary["merged_duplicates"] = dedupe_stats["total_merged"]
    database.set_meta(META_LAST_REFRESH, datetime.now().isoformat(timespec="seconds"))
    _persist()
    return summary


def try_auto_refresh(force=False):
    if not should_refresh(force=force):
        status = refresh_status()
        return {"ran": False, "reason": "fresh_enough", **status}

    if not database.acquire_refresh_lock(minutes=_lock_minutes()):
        return {"ran": False, "reason": "already_running", **refresh_status()}

    try:
        summary = run_refresh()
        status = refresh_status()
        return {"ran": True, **summary, **status}
    finally:
        database.release_refresh_lock(META_REFRESH_LOCK)
