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
from datetime import datetime, date, timezone
from email.utils import parsedate_to_datetime
from dataclasses import dataclass, field

from sector_keywords import match_sectors, make_summary

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


def _normalize(text):
    return re.sub(r"\s+", " ", (text or "")).lower()


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


def fetch_feed(source_name, feed_url, max_items=30, apply_keyword_filter=True):
    articles = []
    parsed = feedparser.parse(feed_url)

    if parsed.bozo and not parsed.entries:
        print(f"  [warning] could not fetch/parse feed: {source_name} ({feed_url})")
        return articles

    for entry in parsed.entries[:max_items]:
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

    return articles


def fetch_all(feeds=None, max_items_per_feed=30):
    feeds = feeds or FEEDS
    apply_filter = keyword_filter_enabled()
    all_articles = []
    raw_total = 0

    for source_name, feed_url in feeds:
        print(f"Fetching RSS: {source_name} ...")
        parsed = feedparser.parse(feed_url)
        raw_count = min(len(parsed.entries or []), max_items_per_feed)
        raw_total += raw_count

        items = fetch_feed(
            source_name, feed_url, max_items_per_feed, apply_keyword_filter=apply_filter
        )
        print(f"  -> {len(items)} kept" + ("" if apply_filter else f" of {raw_count}"))
        all_articles.extend(items)

    stats = {
        "fetched": raw_total,
        "matched": len(all_articles),
        "skipped": max(0, raw_total - len(all_articles)),
        "keyword_filter": apply_filter,
    }
    return all_articles, stats


if __name__ == "__main__":
    arts, stats = fetch_all()
    print(f"\nRSS: {stats['matched']} kept, {stats['skipped']} skipped (filter={'on' if stats['keyword_filter'] else 'off'})")
    for a in arts[:5]:
        print(f"- [{', '.join(a.sectors)}] {a.title}")
