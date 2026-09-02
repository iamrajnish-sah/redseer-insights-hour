"""
rss_ingest.py

Pulls articles from RSS feeds. By default, only keeps headlines that match
your sector keywords (see SECTOR_KEYWORDS). Matching items are tagged
immediately — no Gemini needed for RSS.

Set RSS_KEYWORD_FILTER=false to store every RSS item (old behaviour).
"""

import os
import re
import html
import urllib.parse
import feedparser
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timezone
from email.utils import parsedate_to_datetime
from dataclasses import dataclass, field

from sector_keywords import match_sectors, make_summary, GOOGLE_NEWS_QUERIES, is_india_relevant

# Browser-like UA: Business Standard (Akamai) returns 403 to custom bot UAs.
RSS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
RSS_HEADERS = {
    "User-Agent": RSS_USER_AGENT,
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    "Accept-Language": "en-IN,en;q=0.9",
}

# Add the RSS feeds you want to track here.
FEEDS = [
    ("LiveMint - Companies", "https://www.livemint.com/rss/companies"),
    ("LiveMint - Markets", "https://www.livemint.com/rss/markets"),
    ("Economic Times - Tech", "https://economictimes.indiatimes.com/tech/rssfeeds/13357270.cms"),
    ("Economic Times - Markets", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Moneycontrol - Business", "https://www.moneycontrol.com/rss/business.xml"),
    ("The Hindu - Business", "https://www.thehindu.com/business/feeder/default.rss"),
    ("Business Standard - Latest", "https://www.business-standard.com/rss/latest.rss"),
    ("Business Standard - Companies", "https://www.business-standard.com/rss/companies-101.rss"),
    ("Indian Express - Business", "https://indianexpress.com/section/business/feed/"),
    ("Times of India - Business", "https://timesofindia.indiatimes.com/rssfeeds/1898055.cms"),
    ("NDTV Profit", "https://feeds.feedburner.com/ndtvprofit-latest"),
    ("YourStory", "https://yourstory.com/feed/"),
    ("Inc42", "https://inc42.com/feed/"),
    ("Entrackr", "https://entrackr.com/rss"),
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
    resolved_url: str = None


def keyword_filter_enabled():
    return os.environ.get("RSS_KEYWORD_FILTER", "true").lower() not in ("0", "false", "no")


def _max_items_per_feed():
    """How many recent headlines to scan per Indian publisher RSS feed. Default 80."""
    return int(os.environ.get("RSS_MAX_ITEMS", "80"))


def _google_news_max_items():
    """Google News feeds are capped lower so publisher RSS dominates. Default 18."""
    return int(os.environ.get("GOOGLE_NEWS_MAX_ITEMS", "18"))


def _google_news_article_cap():
    """Max Google News articles kept per full refresh. Default 35."""
    return int(os.environ.get("GOOGLE_NEWS_ARTICLE_CAP", "35"))


def _google_news_url(query):
    encoded = urllib.parse.quote(query)
    return f"https://news.google.com/rss/search?q={encoded}&hl=en-IN&gl=IN&ceid=IN:en"


def all_feeds(include_google_news=True):
    """Indian publisher RSS + optional Google News RSS per sector."""
    feeds = list(FEEDS)
    if include_google_news and os.environ.get("GOOGLE_NEWS_RSS", "true").lower() not in ("0", "false", "no"):
        for label, query in GOOGLE_NEWS_QUERIES.items():
            feeds.append((f"Google News — {label}", _google_news_url(query)))
    return feeds


def _max_workers():
    return int(os.environ.get("RSS_MAX_WORKERS", "8"))


def _fetch_timeout():
    return int(os.environ.get("RSS_FETCH_TIMEOUT", "15"))


def _clean_html(raw):
    return html.unescape(re.sub("<[^<]+?>", "", raw or "").strip())


_IMG_SRC_RE = re.compile(r"""<img[^>]+src=['"]([^'"]+)['"]""", re.I)
_OG_FETCH_HEADERS = {
    **RSS_HEADERS,
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_OG_IMAGE_PATTERNS = [
    re.compile(
        r"""<meta[^>]+property=['"]og:image(?::secure_url)?['"][^>]+content=['"]([^'"]+)['"]""",
        re.I,
    ),
    re.compile(
        r"""<meta[^>]+content=['"]([^'"]+)['"][^>]+property=['"]og:image(?::secure_url)?['"]""",
        re.I,
    ),
    re.compile(
        r"""<meta[^>]+name=['"]twitter:image(?::src)?['"][^>]+content=['"]([^'"]+)['"]""",
        re.I,
    ),
    re.compile(
        r"""<meta[^>]+content=['"]([^'"]+)['"][^>]+name=['"]twitter:image(?::src)?['"]""",
        re.I,
    ),
]
_CANONICAL_PATTERNS = [
    re.compile(
        r"""<link[^>]+rel=['"]canonical['"][^>]+href=['"]([^'"]+)['"]""",
        re.I,
    ),
    re.compile(
        r"""<link[^>]+href=['"]([^'"]+)['"][^>]+rel=['"]canonical['"]""",
        re.I,
    ),
    re.compile(
        r"""<meta[^>]+property=['"]og:url['"][^>]+content=['"]([^'"]+)['"]""",
        re.I,
    ),
    re.compile(
        r"""<meta[^>]+content=['"]([^'"]+)['"][^>]+property=['"]og:url['"]""",
        re.I,
    ),
]


def _entry_html_fields(entry):
    """Collect raw HTML fragments from RSS/Atom entry fields."""
    fields = []
    for name in ("summary", "description", "subtitle"):
        value = entry.get(name)
        if value:
            fields.append(value)

    content = entry.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                value = block.get("value")
            else:
                value = getattr(block, "value", None)
            if value:
                fields.append(value)
    elif content:
        fields.append(content)

    return fields


def _is_google_cdn_image(url):
    lowered = (url or "").lower()
    blocked = (
        "googleusercontent.com",
        "ggpht.com",
        "gstatic.com",
        "google.com/images",
        "google.co.in/images",
        "news.google.com",
    )
    return any(token in lowered for token in blocked)


def sanitize_google_news_image(image_url):
    """Google News must use publisher og:image — never Google CDN placeholders."""
    if not image_url or _is_bad_thumbnail(image_url):
        return None
    if _is_google_cdn_image(image_url):
        return None
    return image_url


def _google_thumb_too_small(url):
    """Google CDN size params like =w64-h64 or s0-w128 mean favicon-sized art."""
    lowered = (url or "").lower()
    if "googleusercontent.com" not in lowered and "ggpht.com" not in lowered:
        return False
    sizes = [int(n) for n in re.findall(r"(?:[=/-]w|s0-w)(\d+)", lowered, re.I)]
    sizes += [int(n) for n in re.findall(r"(?:[=/-]h|s0-h)(\d+)", lowered, re.I)]
    if sizes and max(sizes) < 200:
        return True
    if re.search(r"[=/-](?:w|h)\d{1,2}(?:[-_/]|$)", lowered):
        return True
    return False


def _is_bad_thumbnail(url):
    lowered = (url or "").lower()
    if not lowered or lowered.startswith("data:"):
        return True
    junk = (
        "pixel",
        "1x1",
        "spacer",
        "blank.gif",
        "transparent.gif",
        "favicon",
        "emoji",
        "gravatar.com/avatar",
        "placeholder",
        "default-image",
        "no-image",
        "apple-touch-icon",
        "gstatic.com/images/branding",
        "google.com/images/branding",
        "google.com/favicon",
        "news.google.com/images",
        "/logo.",
        "/logo/",
        "publisher-logo",
        "site-icon",
    )
    if any(token in lowered for token in junk):
        return True
    if _google_thumb_too_small(lowered):
        return True
    return False


def _pick_best_image_url(html_fragments):
    candidates = []
    for html in html_fragments:
        for match in _IMG_SRC_RE.finditer(html or ""):
            url = match.group(1).strip()
            if _is_bad_thumbnail(url):
                continue
            candidates.append(url)

    for url in candidates:
        if ("googleusercontent.com" in url or "ggpht.com" in url) and not _google_thumb_too_small(url):
            return url
    for url in candidates:
        if any(token in url for token in ("/wp-content/", "/uploads/", "/images/", "cdn")):
            return url
    return candidates[0] if candidates else None


def _og_per_feed_limit():
    return int(os.environ.get("RSS_OG_PER_FEED", "20"))


def _read_html_head(url):
    """Fetch a page and return (final_url, html_prefix)."""
    if not url:
        return None, ""
    try:
        resp = requests.get(
            url,
            headers=_OG_FETCH_HEADERS,
            timeout=_fetch_timeout(),
            allow_redirects=True,
            stream=True,
        )
        resp.raise_for_status()
        chunk = b""
        for part in resp.iter_content(8192):
            chunk += part
            if len(chunk) >= 98304:
                break
        return resp.url, chunk.decode("utf-8", errors="ignore")
    except Exception:
        return None, ""


def _extract_canonical_url(html_text, current_url):
    for pattern in _CANONICAL_PATTERNS:
        match = pattern.search(html_text or "")
        if match:
            candidate = html.unescape(match.group(1).strip())
            if candidate.startswith(("http://", "https://")):
                return candidate
    if current_url and current_url.startswith(("http://", "https://")):
        return current_url
    return None


def _extract_og_image(html_text):
    for pattern in _OG_IMAGE_PATTERNS:
        match = pattern.search(html_text or "")
        if match:
            image_url = html.unescape(match.group(1).strip())
            if image_url and not _is_bad_thumbnail(image_url):
                return image_url
    return None


def _fetch_article_page_meta(source_url):
    """Resolve redirects and extract og:image from the publisher page."""
    final_url, html_text = _read_html_head(source_url)
    if not final_url:
        return None, None

    resolved_url = _extract_canonical_url(html_text, final_url) or final_url
    image_url = _extract_og_image(html_text)

    if "news.google.com" in resolved_url:
        publisher_hint = None
        for pattern in _CANONICAL_PATTERNS:
            match = pattern.search(html_text or "")
            if match:
                publisher_hint = html.unescape(match.group(1).strip())
                if publisher_hint.startswith(("http://", "https://")):
                    break
                publisher_hint = None
        if publisher_hint and "news.google.com" not in publisher_hint:
            publisher_final, publisher_html = _read_html_head(publisher_hint)
            if publisher_final:
                resolved_url = (
                    _extract_canonical_url(publisher_html, publisher_final) or publisher_final
                )
                image_url = _extract_og_image(publisher_html) or image_url

    return resolved_url, sanitize_google_news_image(image_url)


def _cached_google_news_meta(google_url):
    import database

    key = (google_url or "").strip().rstrip("/")
    if not key:
        return None, None
    cached = database.get_link_cache(key)
    if not cached:
        return None, None
    return cached.get("resolved_url"), sanitize_google_news_image(cached.get("image_url"))


def _resolve_google_news_item(google_url):
    """Resolve a Google News RSS link to publisher URL + og:image, with DB cache."""
    import database

    key = (google_url or "").strip().rstrip("/")
    if not key:
        return None, None

    cached = database.get_link_cache(key)
    if cached:
        return cached.get("resolved_url"), cached.get("image_url")

    resolved_url, image_url = _fetch_article_page_meta(key)
    database.upsert_link_cache(key, resolved_url, image_url)
    return resolved_url, image_url


def _entry_image_url(entry):
    """Best-effort thumbnail from RSS/Atom entry metadata or embedded HTML."""
    media_content = entry.get("media_content") or getattr(entry, "media_content", None) or []
    for item in media_content:
        url = item.get("url")
        medium = (item.get("medium") or item.get("type") or "").lower()
        if url and medium not in ("video", "audio") and not _is_bad_thumbnail(url):
            return url

    media_thumbnail = entry.get("media_thumbnail") or getattr(entry, "media_thumbnail", None) or []
    if media_thumbnail:
        url = media_thumbnail[0].get("url")
        if url and not _is_bad_thumbnail(url):
            return url

    for enc in entry.get("enclosures") or []:
        mime = (enc.get("type") or "").lower()
        href = enc.get("href") or enc.get("url")
        if href and mime.startswith("image/") and not _is_bad_thumbnail(href):
            return href

    for link in entry.get("links") or []:
        mime = (link.get("type") or "").lower()
        href = link.get("href")
        if href and mime.startswith("image/") and not _is_bad_thumbnail(href):
            return href

    image_url = _pick_best_image_url(_entry_html_fields(entry))
    return image_url or None


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


def fetch_feed(source_name, feed_url, max_items=80, apply_keyword_filter=True):
    articles = []
    origin = "google_news" if source_name.startswith("Google News") else "rss"
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
    resolve_budget = [_og_per_feed_limit()] if origin == "google_news" else None
    for entry in entries:
        title = entry.get("title", "").strip()
        if not title:
            continue

        summary = _clean_html(entry.get("summary", "") or entry.get("description", ""))
        link = entry.get("link", "")
        pub_date = _entry_pub_date(entry)

        if origin == "google_news":
            article = Article(
                source=source_name,
                pub_date=pub_date,
                title=title,
                byline=entry.get("author", ""),
                body=summary,
                url=link,
                origin=origin,
            )
            if apply_keyword_filter:
                article = _prepare_article(article)
                if article is None:
                    continue
            if not is_india_relevant(
                article.title,
                article.body,
                article.url,
                getattr(article, "resolved_url", None),
            ):
                continue
            cached_resolved, cached_image = _cached_google_news_meta(link)
            if cached_resolved or cached_image:
                article.resolved_url = cached_resolved
                article.image_url = sanitize_google_news_image(cached_image)
            elif resolve_budget and resolve_budget[0] > 0:
                resolve_budget[0] -= 1
                resolved_url, image_url = _resolve_google_news_item(link)
                article.resolved_url = resolved_url
                article.image_url = sanitize_google_news_image(image_url)
        else:
            article = Article(
                source=source_name,
                pub_date=pub_date,
                title=title,
                byline=entry.get("author", ""),
                body=summary,
                url=link,
                image_url=_entry_image_url(entry),
                origin=origin,
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
    feeds = feeds or all_feeds()
    apply_filter = keyword_filter_enabled()
    publisher_max = max_items_per_feed or _max_items_per_feed()
    google_max = _google_news_max_items()
    all_articles = []
    raw_total = 0
    errors = []
    feed_stats = []
    feeds_ok = 0
    google_count = 0
    google_cap = _google_news_article_cap()

    with ThreadPoolExecutor(max_workers=_max_workers()) as pool:
        futures = {}
        for name, url in feeds:
            per_feed = google_max if name.startswith("Google News") else publisher_max
            futures[pool.submit(
                _fetch_feed_with_retry, name, url, per_feed, apply_filter
            )] = name
        for future in as_completed(futures):
            name = futures[future]
            try:
                items, raw_count = future.result()
                raw_total += raw_count
                if name.startswith("Google News"):
                    remaining = max(0, google_cap - google_count)
                    items = items[:remaining]
                    google_count += len(items)
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
        "max_items_per_feed": publisher_max,
        "google_news_max_items": google_max,
        "google_news_cap": google_cap,
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
