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
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
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
    scrape_meta: dict = field(default_factory=dict)


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


# Players we care about for festive monitoring (platforms + research houses).
FESTIVE_PLAYERS = tuple(
    list(FESTIVE_SALE_PLATFORM_MARKERS)
    + [
        "shopsy", "spoyl", "amazon india", "flipkart minutes", "ajio",
        "redseer", "datum", "unicommerce", "bain", "bcg", "kantar",
        "gokwik", "shiprocket", "eternal", "nykaa", "reliance",
    ]
)

FESTIVE_CONTEXT = (
    "festive", "festival", "diwali", "dussehra", "navratri", "dhanteras",
    "big billion", "bbd", "great indian festival", "end of reason",
    "pink friday", "prime day", "shopping festival", "mega sale",
    "festive sale", "festival sale", "festive barometer", "festive outlook",
    "festive gmv", "sale season", "online sale", "ecommerce sale",
    "e-retail", "online retail", "how india shops", "festive shopping",
    "festive deals", "festive offers", "discount festival",
)


def _festive_player_relevant(title, body="", url=""):
    """Keep only festive-sale / named-player shopping stories — drop generic news."""
    hay = f"{title or ''} {body or ''} {url or ''}".lower()
    if not hay.strip():
        return False
    if is_festive_sale_relevant(title or "", body or ""):
        return True
    if any(k in hay for k in FESTIVE_SALE_KEYWORDS):
        return True
    has_player = any(p in hay for p in FESTIVE_PLAYERS)
    has_festive = any(m in hay for m in FESTIVE_CONTEXT)
    # Research / market reports about festive ecommerce
    if has_festive and any(
        x in hay
        for x in ("gmv", "barometer", "outlook", "e-commerce", "ecommerce", "e-retail", "online retail", "market")
    ):
        return True
    # Platform + explicit sale/festive language (not bare "amazon" alone)
    if has_player and has_festive:
        return True
    if has_player and any(x in hay for x in ("sale", "deals", "discount", "offer", "gmv", "orders")):
        return True
    return False


def _sale_signal(text):
    """Back-compat alias used by older call sites."""
    return _festive_player_relevant(text, "", "")


def _clean_text(value):
    text = unescape(re.sub(r"\s+", " ", value or "")).strip()
    return text


def scrape_website_target(target, max_links=None, fetch_detail=None, deadline=None):
    """Fast festive-only listing scrape. Detail fetches are off by default."""
    max_links = int(max_links or os.environ.get("WEBSITE_SCRAPE_MAX_LINKS", "4"))
    if fetch_detail is None:
        fetch_detail = os.environ.get("WEBSITE_SCRAPE_FETCH_DETAIL", "false").lower() in (
            "1", "true", "yes",
        )
    if deadline is not None and time.monotonic() > deadline:
        return []

    url = target["url"]
    timeout = float(os.environ.get("WEBSITE_SCRAPE_TIMEOUT", "8"))
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    # Also score page title / H1 for report landing pages with few links
    page_heading = _clean_text(
        (soup.title.get_text(" ", strip=True) if soup.title else "")
        + " "
        + " ".join(h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2"])[:4])
    )
    seen = set()
    candidates = []

    # If the landing page itself is a festive report, keep it as one card
    if _festive_player_relevant(page_heading, "", url) and len(page_heading) >= 24:
        candidates.append((page_heading[:300], url))

    for a in soup.find_all("a", href=True):
        if deadline is not None and time.monotonic() > deadline:
            break
        href = urljoin(url, a.get("href"))
        title = _clean_text(a.get_text(" ", strip=True))
        if not title or len(title) < 16:
            continue
        if href in seen:
            continue
        parsed = urlparse(href)
        if parsed.scheme not in ("http", "https"):
            continue
        if not _festive_player_relevant(title, "", href):
            continue
        seen.add(href)
        candidates.append((title, href))
        if len(candidates) >= max_links:
            break

    articles = []
    today = date.today().isoformat()
    label = target.get("label") or urlparse(url).netloc
    for title, href in candidates[:max_links]:
        if deadline is not None and time.monotonic() > deadline:
            break
        body = title
        if fetch_detail and href != url:
            try:
                detail = requests.get(href, headers=HEADERS, timeout=min(6.0, timeout))
                ctype = detail.headers.get("content-type", "")
                if detail.ok and "html" in ctype:
                    page = BeautifulSoup(detail.text, "lxml")
                    for tag in page(["script", "style", "noscript"]):
                        tag.decompose()
                    paras = [
                        _clean_text(p.get_text(" ", strip=True))
                        for p in page.find_all(["p", "h1", "h2"])
                    ]
                    paras = [p for p in paras if len(p) > 40]
                    if paras:
                        body = " ".join(paras[:5])[:1800]
            except requests.RequestException:
                pass
            # Drop after detail if still not festive
            if not _festive_player_relevant(title, body, href):
                continue
        sectors = ["festive_sale"]
        # Keep commerce tag when a marketplace player is named
        extra = match_sectors(title, body)
        for s in extra:
            if s not in sectors and s != "cross_sector":
                sectors.append(s)
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


def _instagram_scrape_all_posts():
    return os.environ.get("INSTAGRAM_SCRAPE_ALL", "true").lower() in ("1", "true", "yes")


def _parse_timestamp(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return _parse_timestamp(int(text))
    try:
        if text.endswith("Z"):
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _ist_date_time(dt):
    if dt is None:
        today = datetime.now(IST)
        return today.date().isoformat(), today.strftime("%H:%M")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(IST)
    return local.date().isoformat(), local.strftime("%H:%M")


def _extract_hashtags(caption, item):
    tags = item.get("hashtags")
    if isinstance(tags, list):
        parts = [str(t).strip().lstrip("#") for t in tags if str(t).strip()]
        if parts:
            return " ".join(f"#{t}" for t in parts)
    found = re.findall(r"#(\w+)", caption or "")
    if found:
        return " ".join(f"#{t}" for t in found)
    return ""


def _posted_by_line(item, profile_handle):
    owner = (item.get("ownerUsername") or item.get("username") or "").strip()
    owner_name = (item.get("ownerFullName") or item.get("fullName") or "").strip()
    tagged = item.get("taggedUsers") or item.get("mentions") or []
    coauthors = item.get("coauthorProducers") or item.get("coAuthorProducers") or []
    candidates = []
    if owner and owner.lower() != profile_handle.lower():
        label = f"{owner} ({owner_name})" if owner_name else owner
        candidates.append(label)
    if isinstance(coauthors, list):
        for co in coauthors:
            if isinstance(co, dict):
                u = (co.get("username") or co.get("ownerUsername") or "").strip()
                n = (co.get("fullName") or co.get("ownerFullName") or "").strip()
                if u:
                    candidates.append(f"{u} ({n})" if n else u)
            elif isinstance(co, str) and co.strip():
                candidates.append(co.strip())
    if isinstance(tagged, list):
        for tag in tagged[:3]:
            if isinstance(tag, dict):
                u = (tag.get("username") or "").strip()
                n = (tag.get("full_name") or tag.get("fullName") or "").strip()
                if u and u.lower() != profile_handle.lower():
                    candidates.append(f"{u} ({n})" if n else u)
            elif isinstance(tag, str) and tag.strip():
                candidates.append(tag.strip().lstrip("@"))
    if candidates:
        return candidates[0]
    if owner_name:
        return f"{profile_handle} ({owner_name})"
    return profile_handle


def _paid_partnership(caption, item):
    if item.get("isSponsored") or item.get("paidPartnership") or item.get("isPaidPartnership"):
        return "Yes"
    cap = (caption or "").lower()
    if "#collab" in cap or "paid partnership" in cap or "#ad" in cap:
        return "Yes"
    return "No"


def _infer_content_type(caption, item, profile_handle, pinned, is_repost):
    cap = (caption or "").lower()
    if pinned and any(x in cap for x in ("big billion", "bbd", "launch", "yahaan")):
        return "Brand film - main launch (pinned)"
    if any(x in cap for x in ("#collab", "collab")) or item.get("coauthorProducers"):
        return "Brand film - celebrity collab"
    suffix = " (repost)" if is_repost else ""
    category_rules = [
        ("Premium smartphones", ("smartphone", "iphone", "galaxy", "pixel", "oneplus", "mobile")),
        ("Laptops", ("laptop", "macbook", "notebook", "chromebook")),
        ("Camera phones", ("camera phone", "camera", "photography", "megapixel")),
        ("Beauty & skincare", ("beauty", "skincare", "makeup", "serum", "lipstick")),
        ("Fashion", ("fashion", "outfit", "wardrobe", "ethnic wear")),
        ("TVs", (" tv", "television", "oled", "qled")),
        ("Earbuds", ("earbud", "earphone", "headphone", "tws")),
        ("Sneakers", ("sneaker", "shoe", "footwear")),
    ]
    for label, keywords in category_rules:
        if any(k in cap for k in keywords):
            return f"Category - {label}{suffix}"
    post_type = (item.get("type") or item.get("productType") or "").lower()
    if post_type in ("video", "reel", "clips"):
        return f"Brand film{suffix}" if not suffix else f"Category - Video{suffix}"
    if "delivery" in cap or "blinkit" in cap or "minutes" in cap:
        return "Brand - Delivery partner humour"
    return f"Category - General{suffix}" if is_repost else "Brand - Social post"


def _instagram_item_to_article(item, handle, label, sector):
    caption_raw = item.get("caption") or item.get("text") or item.get("alt") or ""
    if isinstance(caption_raw, dict):
        caption_raw = caption_raw.get("text") or ""
    caption = _clean_text(str(caption_raw))
    short = caption[:120] + ("…" if len(caption) > 120 else "")
    title = short or f"Instagram post from @{handle}"
    post_url = (
        item.get("url")
        or item.get("postUrl")
        or item.get("link")
    )
    if not post_url and item.get("shortCode"):
        post_url = f"https://www.instagram.com/p/{item['shortCode']}/"
    if not post_url:
        post_url = _instagram_url(handle)
    image = (
        item.get("displayUrl")
        or item.get("imageUrl")
        or item.get("thumbnailUrl")
    )
    if not image:
        images = item.get("images")
        if isinstance(images, list) and images:
            image = images[0]
    timestamp = (
        item.get("timestamp")
        or item.get("takenAt")
        or item.get("takenAtTimestamp")
        or item.get("time")
    )
    dt = _parse_timestamp(timestamp)
    pub_date, time_ist = _ist_date_time(dt)
    owner = (item.get("ownerUsername") or item.get("username") or handle).strip()
    is_repost = owner.lower() != handle.lower()
    pinned = bool(item.get("isPinned") or item.get("pinned"))
    likes = (
        item.get("likesCount")
        or item.get("likeCount")
        or item.get("likes")
        or 0
    )
    comments = item.get("commentsCount") or item.get("commentCount") or item.get("comments") or 0
    views = item.get("videoViewCount") or item.get("viewCount") or item.get("views") or ""
    plays = item.get("videoPlayCount") or item.get("playCount") or item.get("plays") or ""
    duration = item.get("videoDuration") or item.get("duration") or item.get("videoDurationSec")
    if duration is None and item.get("videoDurationMs"):
        try:
            duration = float(item["videoDurationMs"]) / 1000.0
        except (TypeError, ValueError):
            duration = None
    hashtags = _extract_hashtags(caption, item)
    posted_by = _posted_by_line(item, handle)
    paid = _paid_partnership(caption, item)
    content_type = _infer_content_type(caption, item, handle, pinned, is_repost)
    scrape_meta = {
        "profile_handle": handle,
        "profile_label": label,
        "date_ist": pub_date,
        "time_ist": time_ist,
        "posted_by": posted_by,
        "paid_partnership": paid,
        "pinned": "Yes" if pinned else "No",
        "content_type": content_type,
        "hashtags": hashtags,
        "duration_sec": duration if duration not in ("", None) else "",
        "likes": likes,
        "comments": comments,
        "views": views if views not in ("", None) else "",
        "plays": plays if plays not in ("", None) else "",
        "post_url": post_url,
        "cover_image": image if isinstance(image, str) else "",
    }
    sectors = [sector] if sector else []
    return Article(
        source=label,
        pub_date=pub_date,
        title=title[:300],
        subtitle=content_type[:200],
        body=caption[:8000],
        origin="instagram",
        url=post_url,
        resolved_url=post_url,
        image_url=image if isinstance(image, str) else None,
        page=handle,
        article_id=str(item.get("id") or item.get("shortCode") or "") or None,
        sectors=sectors,
        pre_classified=True,
        auto_summary=make_summary(title, caption),
        scrape_meta=scrape_meta,
    )


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
    max_posts = int(max_posts or os.environ.get("INSTAGRAM_SCRAPE_MAX_POSTS", "30"))
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
    items = _apify_run_sync(actor, payload, wait_secs=int(os.environ.get("APIFY_WAIT_SECS", "120")))
    articles = []
    sector = (target.get("sector") or "").strip()
    # Keep IG posts out of the Festive Sale news strip — they only appear in the Instagram hub.
    if sector == "festive_sale":
        sector = "instagram_monitoring"
    label = target.get("label") or f"@{handle}"
    scrape_all = _instagram_scrape_all_posts()
    for item in items[: max_posts * 3 if not scrape_all else max_posts]:
        if not isinstance(item, dict):
            continue
        article = _instagram_item_to_article(item, handle, label, sector)
        if not scrape_all and not _festive_player_relevant(
            article.title, article.body, article.url or ""
        ):
            continue
        articles.append(article)
        if len(articles) >= max_posts:
            break
    return articles


def _scrape_budget_seconds():
    default = "45" if os.environ.get("VERCEL") else "90"
    return float(os.environ.get("SCRAPE_BUDGET_SECONDS", default))


def run_target(target_id, deadline=None):
    target = get_target(target_id)
    if not target:
        raise LookupError("Scrape target not found")
    kind = target["kind"]
    try:
        if kind == KIND_WEBSITE:
            articles = scrape_website_target(target, deadline=deadline)
        elif kind == KIND_INSTAGRAM:
            if deadline is not None and time.monotonic() > deadline:
                return {
                    "ok": False,
                    "target_id": target_id,
                    "kind": kind,
                    "label": target.get("label"),
                    "skipped": True,
                    "reason": "time_budget",
                }
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
    """Run enabled targets with a hard time budget (festive-only results)."""
    targets = list_targets(kind=kind, enabled_only=True)
    budget = _scrape_budget_seconds()
    deadline = time.monotonic() + budget
    results = []
    stopped_early = False
    for target in targets:
        if time.monotonic() > deadline:
            stopped_early = True
            results.append(
                {
                    "ok": False,
                    "target_id": target["id"],
                    "kind": target["kind"],
                    "label": target.get("label"),
                    "skipped": True,
                    "reason": "time_budget",
                }
            )
            continue
        try:
            results.append(run_target(target["id"], deadline=deadline))
        except Exception as exc:
            results.append(
                {
                    "ok": False,
                    "target_id": target["id"],
                    "kind": target["kind"],
                    "label": target.get("label"),
                    "error": str(exc)[:240],
                }
            )
    inserted = sum(int(r.get("inserted") or 0) for r in results if r.get("ok"))
    refreshed = sum(int(r.get("refreshed") or 0) for r in results if r.get("ok"))
    fetched = sum(int(r.get("fetched") or 0) for r in results if r.get("ok"))
    return {
        "ran": len(results),
        "ok_count": sum(1 for r in results if r.get("ok")),
        "inserted": inserted,
        "refreshed": refreshed,
        "fetched": fetched,
        "stopped_early": stopped_early,
        "budget_seconds": budget,
        "festive_only": kind != KIND_INSTAGRAM,
        "results_location": (
            "Instagram scrape box (Excel export)"
            if kind == KIND_INSTAGRAM
            else "Website scrape box + Festive Sale tab"
        ),
        "results": results,
        "apify_configured": apify_configured(),
        "message": (
            f"Festive scrape done — {fetched} relevant items, {inserted} new, "
            f"{refreshed} updated. Open the Festive Sale tab to view."
            + (" (stopped early on time budget; run again for remaining sites)" if stopped_early else "")
        ),
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
