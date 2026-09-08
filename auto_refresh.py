"""
auto_refresh.py

Keeps the dashboard up to date without manual button clicks:
  - Vercel Cron (daily full refresh + a second RSS-only run)
  - On visit, pull only the sources that are stale

RSS is free and can run often. GNews (100 req/day) and NewsAPI are
metered, so they refresh less frequently.
"""

import os
from datetime import datetime, timedelta, timezone
import time

import database
import dedupe
import rss_ingest
import gnews_ingest
import newsapi_ingest

META_LAST_REFRESH = "last_auto_refresh"
META_LAST_RSS = "last_rss_refresh"
META_LAST_GNEWS = "last_gnews_refresh"
META_LAST_NEWSAPI = "last_newsapi_refresh"
META_REFRESH_LOCK = "refresh_lock_until"


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _utc_iso(dt=None):
    return (dt or _utcnow()).isoformat(timespec="seconds") + "Z"


def _parse_ts(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", ""))
    except ValueError:
        return None


def _hours(env_name, default):
    return float(os.environ.get(env_name, str(default)))


def rss_refresh_hours():
    return _hours("RSS_REFRESH_HOURS", 2)


def gnews_refresh_hours():
    return _hours("GNEWS_REFRESH_HOURS", 4)


def newsapi_refresh_hours():
    return _hours("NEWSAPI_REFRESH_HOURS", 6)


def _lock_minutes():
    return int(os.environ.get("AUTO_REFRESH_LOCK_MINUTES", "15"))


def _source_stale(meta_key, hours):
    last = _parse_ts(database.get_meta(meta_key))
    if not last:
        return True
    return _utcnow() - last >= timedelta(hours=hours)


def due_sources():
    has_gnews = bool(os.environ.get("GNEWSAPIKEY") or os.environ.get("GNEWS_API_KEY"))
    has_newsapi = bool(os.environ.get("NEWSAPIKEY") or os.environ.get("NEWSAPI_KEY"))
    return {
        "rss": _source_stale(META_LAST_RSS, rss_refresh_hours()),
        "gnews": has_gnews and _source_stale(META_LAST_GNEWS, gnews_refresh_hours()),
        "newsapi": has_newsapi and _source_stale(META_LAST_NEWSAPI, newsapi_refresh_hours()),
    }


def get_last_refresh():
    return _parse_ts(database.get_meta(META_LAST_REFRESH))


def should_refresh(force=False):
    if force:
        return True
    if database.get_relevant_count() == 0:
        return True
    return any(due_sources().values())


def _source_status(meta_key, hours, configured=True):
    last = _parse_ts(database.get_meta(meta_key))
    return {
        "configured": configured,
        "last_refresh": _utc_iso(last) if last else None,
        "refresh_hours": hours,
        "stale": (not configured) or _source_stale(meta_key, hours) if configured else False,
    }


def refresh_status():
    has_gnews = bool(os.environ.get("GNEWSAPIKEY") or os.environ.get("GNEWS_API_KEY"))
    has_newsapi = bool(os.environ.get("NEWSAPIKEY") or os.environ.get("NEWSAPI_KEY"))
    sources = {
        "rss": {
            **_source_status(META_LAST_RSS, rss_refresh_hours()),
            "free": True,
            "note": "Publisher RSS — no API quota, pulled every 2 hours on visit plus twice-daily cron.",
        },
        "gnews": {
            **_source_status(META_LAST_GNEWS, gnews_refresh_hours(), configured=has_gnews),
            "free": False,
            "note": "GNews.io free tier ~100 requests/day, 1 req/sec — every 4 hours.",
        },
        "newsapi": {
            **_source_status(META_LAST_NEWSAPI, newsapi_refresh_hours(), configured=has_newsapi),
            "free": False,
            "note": "NewsAPI metered — every 6 hours when a key is set.",
        },
    }
    last = get_last_refresh()
    return {
        "stale": should_refresh(force=False),
        "last_refresh": _utc_iso(last) if last else None,
        "article_count": database.get_relevant_count(),
        "refresh_hours": rss_refresh_hours(),
        "sources": sources,
        "cron_full": "0 8 * * *",
        "cron_rss": "0 14 * * *",
    }


def _persist():
    from db_persist import save_db, enabled

    if not enabled():
        return False
    return save_db(database.DB_PATH)


def _refresh_budget_seconds():
    return float(
        os.environ.get(
            "REFRESH_BUDGET_SECONDS",
            "50" if os.environ.get("VERCEL") else "240",
        )
    )


def run_refresh(include_rss=True, include_gnews=True, include_newsapi=None):
    """Pull selected sources and dedupe.

    Refresh is insert-only: new articles are merged in by URL / title+date.
    Existing articles are never deleted or overwritten.
    On Vercel the work is time-budgeted so the function returns before the
    platform kills it with FUNCTION_INVOCATION_TIMEOUT.
    """
    if include_newsapi is None:
        include_newsapi = bool(os.environ.get("NEWSAPIKEY") or os.environ.get("NEWSAPI_KEY"))

    before_total = database.get_total_count()
    before_relevant = database.get_relevant_count()
    print(f"[refresh] before: {before_total} stored, {before_relevant} relevant")

    now = _utc_iso()
    started = time.monotonic()
    budget = _refresh_budget_seconds()

    def remaining():
        return budget - (time.monotonic() - started)

    summary = {
        "rss": None,
        "gnews": None,
        "newsapi": None,
        "total_inserted": 0,
        "total_refreshed": 0,
        "articles_before": before_total,
        "sources_run": [],
        "timed_out_early": False,
    }

    if include_rss:
        articles, stats = rss_ingest.fetch_all()
        inserted, _, refreshed = database.insert_articles(articles)
        summary["rss"] = {"inserted": inserted, "refreshed": len(refreshed), **stats}
        summary["total_inserted"] += inserted
        summary["total_refreshed"] += len(refreshed)
        summary["sources_run"].append("rss")
        database.set_meta(META_LAST_RSS, now)

    gnews_key = os.environ.get("GNEWSAPIKEY") or os.environ.get("GNEWS_API_KEY")
    if include_gnews and gnews_key:
        left = remaining()
        if left < 12:
            summary["gnews"] = {"skipped": True, "reason": "time_budget"}
            summary["timed_out_early"] = True
            print("[refresh] skipping GNews — not enough time left in function budget")
        else:
            os.environ["GNEWS_BUDGET_SECONDS"] = str(max(8, int(left - 10)))
            articles, stats = gnews_ingest.fetch_all()
            inserted, _, refreshed = database.insert_articles(articles)
            summary["gnews"] = {"inserted": inserted, "refreshed": len(refreshed), **stats}
            summary["total_inserted"] += inserted
            summary["total_refreshed"] += len(refreshed)
            summary["sources_run"].append("gnews")
            database.set_meta(META_LAST_GNEWS, now)

    if include_newsapi and (os.environ.get("NEWSAPIKEY") or os.environ.get("NEWSAPI_KEY")):
        left = remaining()
        if left < 10:
            summary["newsapi"] = {"skipped": True, "reason": "time_budget"}
            summary["timed_out_early"] = True
            print("[refresh] skipping NewsAPI — not enough time left in function budget")
        else:
            os.environ["NEWSAPI_BUDGET_SECONDS"] = str(max(6, int(left - 6)))
            articles, stats = newsapi_ingest.fetch_all()
            inserted, _, refreshed = database.insert_articles(articles)
            summary["newsapi"] = {"inserted": inserted, "refreshed": len(refreshed), **stats}
            summary["total_inserted"] += inserted
            summary["total_refreshed"] += len(refreshed)
            summary["sources_run"].append("newsapi")
            database.set_meta(META_LAST_NEWSAPI, now)

    dedupe_stats = dedupe.run_all_dedupes()
    summary["merged_duplicates"] = dedupe_stats["total_merged"]
    database.set_meta(META_LAST_REFRESH, now)

    after_total = database.get_total_count()
    after_relevant = database.get_relevant_count()
    summary["articles_after"] = after_total
    print(
        f"[refresh] after: {after_total} stored, {after_relevant} relevant — "
        f"{summary['total_inserted']} inserted, {summary['total_refreshed']} refreshed, "
        f"{dedupe_stats['total_merged']} marked duplicate; ran={summary['sources_run']}"
    )
    if after_total < before_total:
        print(
            f"[refresh][WARNING] stored article count DECREASED {before_total} -> {after_total} — "
            f"this should never happen; investigate immediately"
        )

    _persist()
    return summary


def try_auto_refresh(force=False, rss_only=False):
    # Opening the dashboard must not run GNews + NewsAPI + RSS in one 60s
    # function — that is what triggers FUNCTION_INVOCATION_TIMEOUT.
    if os.environ.get("VERCEL") and not force:
        rss_only = True
    due = due_sources()
    if rss_only:
        include = {"rss": True if force else due["rss"], "gnews": False, "newsapi": False}
    elif force:
        include = {"rss": True, "gnews": True, "newsapi": True}
    else:
        include = due

    if not any(include.values()):
        status = refresh_status()
        return {"ran": False, "reason": "fresh_enough", **status}

    if not database.acquire_refresh_lock(minutes=_lock_minutes()):
        return {"ran": False, "reason": "already_running", **refresh_status()}

    try:
        summary = run_refresh(
            include_rss=include["rss"],
            include_gnews=include["gnews"],
            include_newsapi=include["newsapi"],
        )
        status = refresh_status()
        return {"ran": True, **summary, **status}
    finally:
        database.release_refresh_lock(META_REFRESH_LOCK)
