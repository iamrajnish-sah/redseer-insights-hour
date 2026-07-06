"""
newsapi_ingest.py

Pulls sector-targeted articles from NewsAPI.org. Results are tagged immediately
(keyword + query sector) — no Gemini needed.

Requires: NEWSAPIKEY environment variable (https://newsapi.org)

IMPORTANT: Free NewsAPI keys only allow /everything from localhost. On Vercel we
use /top-headlines?country=in (works in production).
"""

import os
import time
import requests
from dataclasses import dataclass, field
from datetime import date, timedelta

from sector_keywords import NEWSAPI_HEADLINE_QUERIES, NEWSAPI_QUERIES, match_sectors, make_summary
from dedupe import normalize_title

NEWSAPI_EVERYTHING_URL = "https://newsapi.org/v2/everything"
NEWSAPI_TOP_URL = "https://newsapi.org/v2/top-headlines"


@dataclass
class Article:
    source: str
    pub_date: str
    title: str
    subtitle: str = ""
    byline: str = ""
    body: str = ""
    origin: str = "newsapi"
    url: str = None
    page: str = None
    article_id: str = None
    sectors: list = field(default_factory=list)
    pre_classified: bool = False
    auto_summary: str = ""
    image_url: str = None


def _api_key():
    return (os.environ.get("NEWSAPIKEY") or os.environ.get("NEWSAPI_KEY") or "").strip()


def _page_size():
    return int(os.environ.get("NEWSAPI_PAGE_SIZE", "25"))


def _use_headlines():
    if os.environ.get("NEWSAPI_USE_HEADLINES", "").lower() in ("0", "false", "no"):
        return False
    if os.environ.get("NEWSAPI_USE_HEADLINES", "").lower() in ("1", "true", "yes"):
        return True
    return bool(os.environ.get("VERCEL"))


def _normalize_url(url):
    if not url:
        return None
    return url.strip().rstrip("/") or url.strip()


def _prepare_article(article, query_sector):
    sectors = match_sectors(article.title, article.body, article.subtitle)
    if query_sector == "ride_hailing":
        if "ride_hailing" not in sectors:
            return None
    elif query_sector not in sectors:
        sectors.insert(0, query_sector)
    article.sectors = sectors
    article.pre_classified = True
    article.auto_summary = make_summary(article.title, article.body or article.subtitle)
    return article


def _articles_from_payload(items, query_label, endpoint):
    articles = []
    for item in items:
        title = (item.get("title") or "").strip()
        if not title or title == "[Removed]":
            continue
        pub_date = (item.get("publishedAt") or str(date.today()))[:10]
        source_name = (item.get("source") or {}).get("name") or "Unknown"
        article = Article(
            source=f"{source_name} (NewsAPI: {query_label})",
            pub_date=pub_date,
            title=title,
            subtitle=item.get("description") or "",
            byline=item.get("author") or "",
            body=item.get("content") or item.get("description") or "",
            url=item.get("url"),
            image_url=item.get("urlToImage") or None,
        )
        prepared = _prepare_article(article, query_label)
        if prepared:
            articles.append(prepared)
    return articles


def fetch_query_headlines(query_label, query, api_key, page_size=25):
    params = {
        "country": "in",
        "q": query,
        "pageSize": min(page_size, 100),
        "apiKey": api_key,
    }
    resp = requests.get(NEWSAPI_TOP_URL, params=params, timeout=15)
    if resp.status_code != 200:
        msg = resp.text[:200]
        print(f"  [warning] NewsAPI top-headlines '{query_label}': {resp.status_code} {msg}")
        return [], 0, f"{query_label}: HTTP {resp.status_code} — {msg}"

    payload = resp.json()
    if payload.get("status") == "error":
        err = payload.get("message") or "NewsAPI error"
        return [], 0, f"{query_label}: {err}"

    raw = payload.get("articles") or []
    items = _articles_from_payload(raw, query_label, "top-headlines")
    return items, len(raw), None


def fetch_query_everything(query_label, query, api_key, page_size=25):
    from_date = (date.today() - timedelta(days=int(os.environ.get("NEWSAPI_DAYS_BACK", "7")))).isoformat()
    params = {
        "q": query,
        "language": "en",
        "sortBy": "publishedAt",
        "from": from_date,
        "pageSize": min(page_size, 100),
        "apiKey": api_key,
    }
    resp = requests.get(NEWSAPI_EVERYTHING_URL, params=params, timeout=15)
    if resp.status_code != 200:
        msg = resp.text[:200]
        print(f"  [warning] NewsAPI everything '{query_label}': {resp.status_code} {msg}")
        return [], 0, f"{query_label}: HTTP {resp.status_code} — {msg}"

    payload = resp.json()
    if payload.get("status") == "error":
        err = payload.get("message") or "NewsAPI error"
        return [], 0, f"{query_label}: {err}"

    raw = payload.get("articles") or []
    items = _articles_from_payload(raw, query_label, "everything")
    return items, len(raw), None


def fetch_query(query_label, query, api_key, page_size=25):
    if _use_headlines():
        return fetch_query_headlines(query_label, query, api_key, page_size)
    return fetch_query_everything(query_label, query, api_key, page_size)


def fetch_all(queries=None, page_size=None):
    api_key = _api_key()
    if not api_key:
        print("  [warning] NEWSAPIKEY not set — skipping NewsAPI ingestion.")
        return [], {
            "fetched": 0,
            "raw_from_api": 0,
            "matched": 0,
            "skipped": 0,
            "errors": ["NEWSAPIKEY not set on server"],
            "endpoint": "none",
        }

    if _use_headlines():
        queries = queries or NEWSAPI_HEADLINE_QUERIES
        endpoint = "top-headlines"
    else:
        queries = queries or NEWSAPI_QUERIES
        endpoint = "everything"

    page_size = page_size or _page_size()
    all_articles = []
    seen_urls = set()
    seen_titles = set()
    raw_from_api = 0
    skipped_dupes = 0
    errors = []

    for index, (label, q) in enumerate(queries.items()):
        if index > 0:
            time.sleep(float(os.environ.get("NEWSAPI_REQUEST_DELAY", "0.6")))
        print(f"Fetching NewsAPI ({endpoint}): {label} ...")
        items, raw_count, err = fetch_query(label, q, api_key, page_size)
        raw_from_api += raw_count
        if err:
            errors.append(err)
        kept = 0
        for article in items:
            url = _normalize_url(article.url)
            title_key = normalize_title(article.title)
            if url and url in seen_urls:
                skipped_dupes += 1
                continue
            if title_key and title_key in seen_titles:
                skipped_dupes += 1
                continue
            if url:
                seen_urls.add(url)
            if title_key:
                seen_titles.add(title_key)
            all_articles.append(article)
            kept += 1
        print(f"  -> {kept} kept ({raw_count} from API)")

    stats = {
        "fetched": len(all_articles),
        "raw_from_api": raw_from_api,
        "matched": len(all_articles),
        "skipped": skipped_dupes,
        "errors": errors,
        "endpoint": endpoint,
    }
    return all_articles, stats


if __name__ == "__main__":
    arts, stats = fetch_all()
    print(f"\nNewsAPI ({stats['endpoint']}): {stats['matched']} kept")
    for a in arts[:5]:
        print(f"- [{', '.join(a.sectors)}] {a.title}")
