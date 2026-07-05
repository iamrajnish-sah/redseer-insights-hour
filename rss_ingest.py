"""
rss_ingest.py

Pulls articles from RSS feeds. By default, only keeps headlines that match
your sector keywords (see SECTOR_KEYWORDS). Matching items are tagged
immediately — no Gemini needed for RSS.

Set RSS_KEYWORD_FILTER=false to store every RSS item (old behaviour).
"""

import os
import re
import feedparser
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timezone
from email.utils import parsedate_to_datetime
from dataclasses import dataclass, field

from sector_keywords import match_sectors, make_summary

RSS_USER_AGENT = (
    "RedseerInsightHour/1.0 "
    "(+https://github.com/iamrajnish-sah/redseer-insights-hour)"
)
RSS_HEADERS = {
    "User-Agent": RSS_USER_AGENT,
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}

# Add the RSS feeds you want to track here.
FEEDS = [
    ("LiveMint - Companies", "https://www.livemint.com/rss/companies"),
    ("LiveMint - Markets", "https://www.livemint.com/rss/markets"),
    ("Economic Times - Tech", "https://economictimes.indiatimes.com/tech/rssfeeds/13357270.cms"),
    ("Economic Times - Markets", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Moneycontrol - Business", "https://www.moneycontrol.com/rss/business.xml"),
    ("The Hindu - Business", "https://www.thehindu.com/business/feeder/default.rss"),
    ("Business Standard", "https://www.business-standard.com/rss/home_page_top.rss"),
    ("Financial Express", "https://www.financialexpress.com/feed/"),
    ("Times of India - Business", "https://timesofindia.indiatimes.com/rssfeeds/1898055.cms"),
    ("NDTV Profit", "https://feeds.feedburner.com/ndtvprofit-latest"),
    ("YourStory", "https://yourstory.com/feed/"),
    ("Inc42", "https://inc42.com/feed/"),
    ("Entrackr", "https://entrackr.com/feed"),
]


@dataclass
class Article:
    source: str
    pub_date: str
    title: str
    subtitle: str = ""
    byline: str = ""
    body: str = ""
    origin: str = "rss"
    url: str = None
    page: str = None
    article_id: str = None
    sectors: list = field(default_factory=list)
    pre_classified: bool = False
    auto_summary: str = ""
    image_url: str = None


def keyword_filter_enabled():
    return os.environ.get("RSS_KEYWORD_FILTER", "true").lower() not in ("0", "false", "no")


def _max_items_per_feed():
    """How many recent headlines to scan per feed (not a total cap). Default 50."""
    return int(os.environ.get("RSS_MAX_ITEMS", "50"))


def _max_workers():
    return int(os.environ.get("RSS_MAX_WORKERS", "8"))


def _fetch_timeout():
    return int(os.environ.get("RSS_FETCH_TIMEOUT", "15"))


def _clean_html(raw):
    return re.sub("<[^<]+?>", "", raw or "").strip()


def _entry_image_url(entry):
    """Best-effort thumbnail from RSS/Atom entry metadata or embedded HTML."""
    media_content = entry.get("media_content") or getattr(entry, "media_content", None) or []
    for item in media_content:
        url = item.get("url")
        medium = (item.get("medium") or item.get("type") or "").lower()
        if url and medium not in ("video", "audio"):
            return url

    media_thumbnail = entry.get("media_thumbnail") or getattr(entry, "media_thumbnail", None) or []
    if media_thumbnail:
        url = media_thumbnail[0].get("url")
        if url:
            return url

    for enc in entry.get("enclosures") or []:
        mime = (enc.get("type") or "").lower()
        href = enc.get("href") or enc.get("url")
        if href and mime.startswith("image/"):
            return href

    for field_name in ("summary", "description", "content"):
        html = entry.get(field_name) or ""
        if not html:
            continue
        match = re.search(r"""<img[^>]+src=['"]([^'"]+)['"]""", html, re.I)
        if match:
            return match.group(1).strip()

    return None


def _make_summary(title, body, max_len=280):
    return make_summary(title, body, max_len)


def _prepare_article(article):
    sectors = match_sectors(article.title, article.body, article.subtitle)
    if not sectors:
        return None
    article.sectors = sectors
    article.pre_classified = True
    article.auto_summary = _make_summary(article.title, article.body)
    return article


def _entry_pub_date(entry):
    """Use the feed's publish date when available (not today's date)."""
    for field_name in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(field_name)
        if parsed:
            return datetime(*parsed[:6]).strftime("%Y-%m-%d")
    for field_name in ("published", "updated", "created"):
        raw = entry.get(field_name)
        if not raw:
            continue
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo:
                dt = dt.astimezone(timezone.utc)
            return dt.strftime("%Y-%m-%d")
        except (TypeError, ValueError, OverflowError):
            pass
    return str(date.today())


def _download_feed(feed_url):
    """Fetch RSS XML over HTTP (more reliable on Vercel than feedparser.parse(url))."""
    resp = requests.get(
        feed_url,
        headers=RSS_HEADERS,
        timeout=_fetch_timeout(),
    )
    resp.raise_for_status()
    return resp.content


def fetch_feed(source_name, feed_url, max_items=50, apply_keyword_filter=True):
    articles = []
    raw_count = 0

    try:
        content = _download_feed(feed_url)
        parsed = feedparser.parse(content)
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc

    if not parsed.entries:
        if parsed.bozo:
            raise RuntimeError(getattr(parsed, "bozo_exception", "empty or invalid feed"))
        return articles, 0

    entries = parsed.entries[:max_items]
    raw_count = len(entries)
    for entry in entries:
        title = entry.get("title", "").strip()
        if not title:
            continue

        summary = _clean_html(entry.get("summary", "") or entry.get("description", ""))
        link = entry.get("link", "")
        pub_date = _entry_pub_date(entry)

        article = Article(
            source=source_name,
            pub_date=pub_date,
            title=title,
            byline=entry.get("author", ""),
            body=summary,
            url=link,
            image_url=_entry_image_url(entry),
        )
        if apply_keyword_filter:
            article = _prepare_article(article)
            if article is None:
                continue
        articles.append(article)

    return articles, raw_count


def _fetch_feed_with_retry(source_name, feed_url, max_items, apply_filter):
    last_error = None
    for attempt in range(2):
        try:
            return fetch_feed(source_name, feed_url, max_items, apply_filter)
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                print(f"  [info] RSS retry for {source_name} ...")
    raise last_error


def fetch_all(feeds=None, max_items_per_feed=None):
    feeds = feeds or FEEDS
    apply_filter = keyword_filter_enabled()
    max_items = max_items_per_feed or _max_items_per_feed()
    all_articles = []
    raw_total = 0
    errors = []
    feed_stats = []
    feeds_ok = 0

    with ThreadPoolExecutor(max_workers=_max_workers()) as pool:
        futures = {
            pool.submit(
                _fetch_feed_with_retry, name, url, max_items, apply_filter
            ): name
            for name, url in feeds
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                items, raw_count = future.result()
                raw_total += raw_count
                all_articles.extend(items)
                feeds_ok += 1
                feed_stats.append(
                    {"feed": name, "scanned": raw_count, "matched": len(items), "ok": True}
                )
                print(f"  RSS {name}: {len(items)} matched of {raw_count} scanned")
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                feed_stats.append(
                    {"feed": name, "scanned": 0, "matched": 0, "ok": False, "error": str(exc)}
                )
                print(f"  [warning] RSS failed for {name}: {exc}")

    stats = {
        "fetched": raw_total,
        "matched": len(all_articles),
        "skipped": max(0, raw_total - len(all_articles)),
        "keyword_filter": apply_filter,
        "feeds_total": len(feeds),
        "feeds_ok": feeds_ok,
        "feeds_failed": len(errors),
        "max_items_per_feed": max_items,
        "errors": errors,
        "feed_stats": feed_stats,
    }
    return all_articles, stats


if __name__ == "__main__":
    arts, stats = fetch_all()
    print(
        f"\nRSS: scanned {stats['fetched']} headlines from "
        f"{stats['feeds_ok']}/{stats['feeds_total']} feeds, "
        f"{stats['matched']} matched keywords"
    )
    for a in arts[:5]:
        print(f"- [{', '.join(a.sectors)}] {a.title}")
