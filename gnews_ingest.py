"""
gnews_ingest.py

Pulls sector-targeted articles from GNews.io (country=IN).
Results are keyword-filtered and tagged immediately — no Gemini needed.

Requires: GNEWSAPIKEY environment variable (https://gnews.io)
Free tier: 100 requests/day, max 10 articles per request.
"""

import os
import time
import requests
from dataclasses import dataclass, field
from datetime import date

from sector_keywords import GNEWS_QUERIES, match_sectors, make_summary
from dedupe import normalize_title

GNEWS_URL = "https://gnews.io/api/v4/search"


@dataclass
class Article:
    source: str
    pub_date: str
    title: str
    subtitle: str = ""
    byline: str = ""
    body: str = ""
    origin: str = "gnews"
    url: str = None
    page: str = None
    article_id: str = None
    sectors: list = field(default_factory=list)
    pre_classified: bool = False
    auto_summary: str = ""
    image_url: str = None


def _normalize_url(url):
    if not url:
        return None
    return url.strip().rstrip("/") or url.strip()


def _max_results():
    default = "10" if os.environ.get("VERCEL") else "10"
    return int(os.environ.get("GNEWS_MAX_RESULTS", default))


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


def _request_delay_seconds():
    """GNews free plan: max 1 request per second."""
    return float(os.environ.get("GNEWS_REQUEST_DELAY", "1.15"))


def _error_detail(resp):
    try:
        payload = resp.json()
        if isinstance(payload.get("errors"), dict):
            return "; ".join(f"{k}: {v}" for k, v in payload["errors"].items())
        if isinstance(payload.get("errors"), list):
            return "; ".join(str(item) for item in payload["errors"])
    except ValueError:
        pass
    return (resp.text or "").strip()[:160]


def fetch_query(query_label, query, api_key, max_results=10, language="en", country="in"):
    params = {
        "q": query,
        "lang": language,
        "country": country,
        "max": max_results,
        "apikey": api_key,
    }
    retries = int(os.environ.get("GNEWS_RETRIES", "2"))

    for attempt in range(retries + 1):
        resp = requests.get(GNEWS_URL, params=params, timeout=12)
        if resp.status_code == 200:
            payload = resp.json()
            raw_items = payload.get("articles") or []
            articles = []
            for item in raw_items:
                title = (item.get("title") or "").strip()
                if not title:
                    continue
                pub_date = (item.get("publishedAt") or str(date.today()))[:10]
                source_name = (item.get("source") or {}).get("name") or "Unknown"
                article = Article(
                    source=f"{source_name} (GNews: {query_label})",
                    pub_date=pub_date,
                    title=title,
                    subtitle=item.get("description") or "",
                    byline="",
                    body=item.get("content") or item.get("description") or "",
                    url=item.get("url"),
                    image_url=item.get("image") or None,
                )
                prepared = _prepare_article(article, query_label)
                if prepared:
                    articles.append(prepared)
            return articles, len(raw_items), None

        if resp.status_code in (429, 503) and attempt < retries:
            wait = _request_delay_seconds() * (attempt + 1)
            print(f"  [info] GNews rate limit for '{query_label}', retry in {wait:.1f}s ...")
            time.sleep(wait)
            continue

        detail = _error_detail(resp)
        if resp.status_code == 429:
            label = "rate limit (free plan = 1 request/sec)"
        elif resp.status_code == 403:
            label = "daily quota reached (resets midnight UTC)"
        elif resp.status_code == 400:
            label = "bad query syntax"
        else:
            label = f"HTTP {resp.status_code}"
        msg = f"{query_label}: {label}"
        if detail:
            msg += f" — {detail}"
        print(f"  [warning] GNews error for '{query_label}': {msg}")
        return [], 0, msg

    return [], 0, f"{query_label}: request failed after retries"


def fetch_all(queries=None, max_results=None):
    api_key = os.environ.get("GNEWSAPIKEY") or os.environ.get("GNEWS_API_KEY")
    if not api_key:
        print("  [warning] GNEWSAPIKEY not set — skipping GNews ingestion.")
        return [], {"fetched": 0, "raw_from_api": 0, "matched": 0, "skipped": 0, "errors": ["GNEWSAPIKEY not set"]}

    queries = queries or GNEWS_QUERIES
    max_results = max_results or _max_results()
    all_articles = []
    seen_urls = set()
    seen_titles = set()
    raw_total = 0
    raw_from_api = 0
    skipped_dupes = 0
    errors = []

    for index, (label, q) in enumerate(queries.items()):
        if index > 0:
            time.sleep(_request_delay_seconds())
        print(f"Fetching GNews (IN): {label} ...")
        items, raw_count, err = fetch_query(label, q, api_key, max_results=max_results)
        raw_from_api += raw_count
        raw_total += len(items)
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
        "fetched": raw_total,
        "raw_from_api": raw_from_api,
        "matched": len(all_articles),
        "skipped": skipped_dupes,
        "errors": errors,
    }
    return all_articles, stats


if __name__ == "__main__":
    arts, stats = fetch_all()
    print(f"\nGNews: {stats['matched']} kept, {stats['skipped']} dupes skipped")
    for a in arts[:5]:
        print(f"- [{', '.join(a.sectors)}] {a.title}")
