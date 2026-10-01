"""
scrape_ingest.py

Targeted website + Instagram scraping for Festive Sale monitoring.

- Website: fetch a page, pull article-like links matching sale/brand keywords.
- Instagram: Apify actor when APIFY_TOKEN is set (public profiles).

Targets are stored in SQLite (scrape_targets) and can be managed from Backend
Management. Cron can run all enabled targets on a schedule.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from html import unescape
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import database
from scrape_targets_seed import all_default_targets
from sector_keywords import (
    is_festive_sale_relevant,
    match_sectors,
    make_summary,
    FESTIVE_SALE_KEYWORDS,
    FESTIVE_SALE_PLATFORM_MARKERS,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}

KIND_WEBSITE = "website"
KIND_INSTAGRAM = "instagram"

TARGETS_SCHEMA = """
CREATE TABLE IF NOT EXISTS scrape_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    label TEXT,
    url TEXT NOT NULL,
    handle TEXT,
    sector TEXT DEFAULT 'festive_sale',
    enabled INTEGER DEFAULT 1,
    last_run_at TEXT,
    last_status TEXT,
    last_error TEXT,
    created_at TEXT
);
"""


@dataclass
class Article:
    source: str
    pub_date: str
    title: str
    subtitle: str = ""
    byline: str = ""
    body: str = ""
    origin: str = "website_scrape"
    url: str = None
    page: str = None
    article_id: str = None
    sectors: list = field(default_factory=list)
    pre_classified: bool = False
    auto_summary: str = ""
    image_url: str = None
    resolved_url: str = None


def init_scrape_tables(conn=None):
    if conn is not None:
        conn.executescript(TARGETS_SCHEMA)
        return
    with database.get_conn() as c:
        c.executescript(TARGETS_SCHEMA)


def apify_configured():
    return bool((os.environ.get("APIFY_TOKEN") or os.environ.get("APIFY_API_TOKEN") or "").strip())


def apify_token():
    return (os.environ.get("APIFY_TOKEN") or os.environ.get("APIFY_API_TOKEN") or "").strip()


def apify_instagram_actor():
    return (os.environ.get("APIFY_INSTAGRAM_ACTOR") or "apify~instagram-scraper").strip()


def _utcnow_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat(timespec="seconds") + "Z"


def _normalize_handle(value):
    text = (value or "").strip()
    if not text:
        return ""
    if "instagram.com" in text:
        path = urlparse(text).path.strip("/")
        return path.split("/")[0].lstrip("@") if path else ""
    return text.lstrip("@").split("/")[0]


def _instagram_url(handle_or_url):
    handle = _normalize_handle(handle_or_url)
    if not handle:
        return (handle_or_url or "").strip()
    return f"https://www.instagram.com/{handle}/"


def list_targets(kind=None, enabled_only=False):
    init_scrape_tables()
    query = "SELECT * FROM scrape_targets"
    clauses = []
    params = []
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if enabled_only:
        clauses.append("enabled = 1")
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY kind, id DESC"
    with database.get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def add_target(kind, url, label=None, sector="festive_sale"):
    kind = (kind or "").strip().lower()
    if kind not in (KIND_WEBSITE, KIND_INSTAGRAM):
        raise ValueError("kind must be website or instagram")
    url = (url or "").strip()
    if not url:
        raise ValueError("url is required")
    handle = None
    if kind == KIND_INSTAGRAM:
        handle = _normalize_handle(url)
        if not handle:
            raise ValueError("Instagram handle or profile URL is required")
        url = _instagram_url(handle)
        label = (label or handle).strip()
    else:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("Website URL must start with http:// or https://")
        label = (label or parsed.netloc).strip()
    sector = (sector or "festive_sale").strip() or "festive_sale"
    now = _utcnow_iso()
    init_scrape_tables()
    with database.get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO scrape_targets
               (kind, label, url, handle, sector, enabled, created_at)
               VALUES (?, ?, ?, ?, ?, 1, ?)""",
            (kind, label, url, handle, sector, now),
        )
        target_id = cur.lastrowid
    database.persist()
    return get_target(target_id)


def get_target(target_id):
    init_scrape_tables()
    with database.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM scrape_targets WHERE id = ?", (int(target_id),)
        ).fetchone()
    return dict(row) if row else None


def delete_target(target_id):
    init_scrape_tables()
    with database.get_conn() as conn:
        cur = conn.execute("DELETE FROM scrape_targets WHERE id = ?", (int(target_id),))
        deleted = cur.rowcount
    if deleted:
        database.persist()
    return deleted > 0


def set_target_enabled(target_id, enabled):
    init_scrape_tables()
    with database.get_conn() as conn:
        conn.execute(
            "UPDATE scrape_targets SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, int(target_id)),
        )
    database.persist()
    return get_target(target_id)


def _mark_target(target_id, status, error=None):
    with database.get_conn() as conn:
        conn.execute(
            """UPDATE scrape_targets
               SET last_run_at = ?, last_status = ?, last_error = ?
               WHERE id = ?""",
            (_utcnow_iso(), status, (error or "")[:500] or None, int(target_id)),
        )
    database.persist()


def _sale_signal(text):
    hay = (text or "").lower()
    if is_festive_sale_relevant(hay):
        return True
    markers = list(FESTIVE_SALE_KEYWORDS) + list(FESTIVE_SALE_PLATFORM_MARKERS)
    markers += ["sale", "offer", "discount", "deal", "festive", "diwali", "bbd"]
    return any(m in hay for m in markers if len(m) >= 3)


def _clean_text(value):
    text = unescape(re.sub(r"\s+", " ", value or "")).strip()
    return text


def scrape_website_target(target, max_links=None):
    max_links = int(max_links or os.environ.get("WEBSITE_SCRAPE_MAX_LINKS", "12"))
    url = target["url"]
    resp = requests.get(url, headers=HEADERS, timeout=25)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    seen = set()
    candidates = []
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a.get("href"))
        title = _clean_text(a.get_text(" ", strip=True))
        if not title or len(title) < 18:
            continue
        if href in seen:
            continue
        parsed = urlparse(href)
        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.netloc and urlparse(base).netloc not in parsed.netloc and "news" not in parsed.netloc:
            # allow same site + common news CDNs; skip pure external junk
            if urlparse(base).netloc.split(".")[-2:] != parsed.netloc.split(".")[-2:]:
                continue
        blob = f"{title} {href}"
        if not _sale_signal(blob):
            continue
        seen.add(href)
        candidates.append((title, href))
        if len(candidates) >= max_links * 2:
            break

    articles = []
    today = date.today().isoformat()
    sector = target.get("sector") or "festive_sale"
    label = target.get("label") or urlparse(url).netloc
    for title, href in candidates[:max_links]:
        body = title
        try:
            detail = requests.get(href, headers=HEADERS, timeout=15)
            if detail.ok and "text/html" in detail.headers.get("content-type", ""):
                page = BeautifulSoup(detail.text, "lxml")
                for tag in page(["script", "style", "noscript"]):
                    tag.decompose()
                paras = [
                    _clean_text(p.get_text(" ", strip=True))
                    for p in page.find_all(["p", "h1", "h2"])
                ]
                paras = [p for p in paras if len(p) > 40]
                if paras:
                    body = " ".join(paras[:8])[:2500]
        except requests.RequestException:
            pass
        sectors = match_sectors(title, body)
        if sector not in sectors:
            sectors.insert(0, sector)
        if "festive_sale" not in sectors and is_festive_sale_relevant(title, body):
            sectors.insert(0, "festive_sale")
        articles.append(
            Article(
                source=label,
                pub_date=today,
                title=title[:300],
                body=body,
                origin="website_scrape",
                url=href,
                resolved_url=href,
                sectors=sectors,
                pre_classified=True,
                auto_summary=make_summary(title, body),
            )
        )
    return articles


def _apify_run_sync(actor_id, payload, wait_secs=90):
    token = apify_token()
    if not token:
        raise RuntimeError("APIFY_TOKEN is not configured")
    actor_id = actor_id.replace("/", "~")
    start_url = f"https://api.apify.com/v2/acts/{actor_id}/runs?token={token}&waitForFinish={int(wait_secs)}"
    resp = requests.post(start_url, json=payload, timeout=wait_secs + 30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Apify start failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json().get("data") or {}
    status = data.get("status")
    if status != "SUCCEEDED":
        # waitForFinish may still return RUNNING if timed out
        run_id = data.get("id")
        if run_id and status in ("RUNNING", "READY"):
            deadline = time.time() + wait_secs
            while time.time() < deadline:
                time.sleep(4)
                check = requests.get(
                    f"https://api.apify.com/v2/actor-runs/{run_id}?token={token}",
                    timeout=20,
                )
                check.raise_for_status()
                data = check.json().get("data") or {}
                status = data.get("status")
                if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
                    break
        if status != "SUCCEEDED":
            raise RuntimeError(f"Apify run did not succeed (status={status})")
    dataset_id = data.get("defaultDatasetId")
    if not dataset_id:
        raise RuntimeError("Apify run produced no dataset")
    items_resp = requests.get(
        f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={token}&format=json",
        timeout=60,
    )
    items_resp.raise_for_status()
    items = items_resp.json()
    return items if isinstance(items, list) else []


def scrape_instagram_target(target, max_posts=None):
    if not apify_configured():
        raise RuntimeError(
            "Instagram scraping needs APIFY_TOKEN. Add a free Apify token in env, "
            "then re-run. Website scraping works without Apify."
        )
    max_posts = int(max_posts or os.environ.get("INSTAGRAM_SCRAPE_MAX_POSTS", "10"))
    handle = target.get("handle") or _normalize_handle(target.get("url"))
    profile_url = _instagram_url(handle)
    actor = apify_instagram_actor()
    if "instagram-posts-scraper-lowcost" in actor or "sones" in actor:
        payload = {
            "usernames": [handle],
            "postsPerProfile": max_posts,
        }
    else:
        payload = {
            "directUrls": [profile_url],
            "resultsType": "posts",
            "resultsLimit": max_posts,
            "searchLimit": max_posts,
        }
    items = _apify_run_sync(actor, payload, wait_secs=int(os.environ.get("APIFY_WAIT_SECS", "100")))
    articles = []
    today = date.today().isoformat()
    sector = target.get("sector") or "festive_sale"
    label = target.get("label") or f"@{handle}"
    for item in items[:max_posts]:
        caption = (
            item.get("caption")
            or item.get("text")
            or item.get("alt")
            or ""
        )
        if isinstance(caption, dict):
            caption = caption.get("text") or ""
        caption = _clean_text(str(caption))
        short = caption[:120] + ("…" if len(caption) > 120 else "")
        title = short or f"Instagram post from @{handle}"
        post_url = (
            item.get("url")
            or item.get("postUrl")
            or item.get("link")
            or profile_url
        )
        image = (
            item.get("displayUrl")
            or item.get("imageUrl")
            or (item.get("images") or [None])[0]
        )
        timestamp = item.get("timestamp") or item.get("takenAt") or item.get("time")
        pub_date = today
        if timestamp:
            try:
                if isinstance(timestamp, (int, float)):
                    pub_date = datetime.utcfromtimestamp(timestamp).date().isoformat()
                else:
                    pub_date = str(timestamp)[:10]
            except (ValueError, OSError, TypeError):
                pub_date = today
        if not _sale_signal(f"{title} {caption}") and not is_festive_sale_relevant(title, caption):
            # Still keep brand posts from tracked festive accounts — lightly tag
            sectors = [sector]
        else:
            sectors = match_sectors(title, caption)
            if sector not in sectors:
                sectors.insert(0, sector)
            if "festive_sale" not in sectors:
                sectors.insert(0, "festive_sale")
        articles.append(
            Article(
                source=label,
                pub_date=pub_date,
                title=title[:300],
                body=caption[:3000],
                origin="instagram",
                url=post_url,
                resolved_url=post_url,
                image_url=image if isinstance(image, str) else None,
                sectors=sectors,
                pre_classified=True,
                auto_summary=make_summary(title, caption),
            )
        )
    return articles


def run_target(target_id):
    target = get_target(target_id)
    if not target:
        raise LookupError("Scrape target not found")
    kind = target["kind"]
    try:
        if kind == KIND_WEBSITE:
            articles = scrape_website_target(target)
        elif kind == KIND_INSTAGRAM:
            articles = scrape_instagram_target(target)
        else:
            raise ValueError(f"Unsupported kind: {kind}")
        inserted, ids, refreshed = database.insert_articles(articles)
        _mark_target(target_id, "ok", None)
        return {
            "ok": True,
            "target_id": target_id,
            "kind": kind,
            "label": target.get("label"),
            "fetched": len(articles),
            "inserted": inserted,
            "refreshed": len(refreshed),
            "ids": ids,
        }
    except Exception as exc:
        _mark_target(target_id, "error", str(exc))
        raise


def run_all_enabled(kind=None):
    targets = list_targets(kind=kind, enabled_only=True)
    results = []
    for target in targets:
        try:
            results.append(run_target(target["id"]))
        except Exception as exc:
            results.append(
                {
                    "ok": False,
                    "target_id": target["id"],
                    "kind": target["kind"],
                    "label": target.get("label"),
                    "error": str(exc),
                }
            )
    return {
        "ran": len(results),
        "ok_count": sum(1 for r in results if r.get("ok")),
        "results": results,
        "apify_configured": apify_configured(),
    }


def status_summary():
    targets = list_targets()
    return {
        "apify_configured": apify_configured(),
        "apify_instagram_actor": apify_instagram_actor() if apify_configured() else None,
        "website_count": sum(1 for t in targets if t["kind"] == KIND_WEBSITE),
        "instagram_count": sum(1 for t in targets if t["kind"] == KIND_INSTAGRAM),
        "enabled_count": sum(1 for t in targets if t.get("enabled")),
        "targets": targets,
        "default_seed_count": len(all_default_targets()),
    }


def _target_key(kind, url, handle=None):
    kind = (kind or "").strip().lower()
    if kind == KIND_INSTAGRAM:
        return (KIND_INSTAGRAM, _normalize_handle(handle or url).lower())
    return (KIND_WEBSITE, (url or "").strip().rstrip("/").lower())


def seed_default_targets(force=False):
    """Insert curated research/newsroom/Instagram targets if missing."""
    init_scrape_tables()
    existing = list_targets()
    seen = {_target_key(t["kind"], t.get("url"), t.get("handle")) for t in existing}
    added = []
    skipped = 0
    for kind, label, url in all_default_targets():
        key = _target_key(kind, url, url if kind == KIND_INSTAGRAM else None)
        if key in seen and not force:
            skipped += 1
            continue
        if key in seen and force:
            skipped += 1
            continue
        try:
            target = add_target(kind=kind, url=url, label=label, sector="festive_sale")
            added.append(target)
            seen.add(key)
        except ValueError as exc:
            skipped += 1
            print(f"  [scrape seed] skip {label}: {exc}")
    return {
        "ok": True,
        "added": len(added),
        "skipped": skipped,
        "total_defaults": len(all_default_targets()),
        "targets_added": [
            {"id": t["id"], "kind": t["kind"], "label": t.get("label"), "url": t.get("url")}
            for t in added
        ],
    }
