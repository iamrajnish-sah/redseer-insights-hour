"""
gnews_ingest.py

Pulls sector-targeted articles from GNews.io (country=IN).
Results are keyword-filtered and tagged immediately — no Gemini needed.

Requires: GNEWSAPIKEY environment variable (https://gnews.io)
Free tier: 100 requests/day, max 10 articles per request.
"""

import os
import requests
from dataclasses import dataclass, field
from datetime import date

from sector_keywords import NEWSAPI_QUERIES, match_sectors, make_summary
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


def fetch_query(query_label, query, api_key, max_results=10, language="en", country="in"):
    params = {
        "q": query,
        "lang": language,
        "country": country,
        "max": max_results,
        "apikey": api_key,
    }
    resp = requests.get(GNEWS_URL, params=params, timeout=15)
    if resp.status_code != 200:
        print(f"  [warning] GNews error for '{query_label}': {resp.status_code} {resp.text[:200]}")
        return []

    articles = []
    for item in resp.json().get("articles", []):
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
    return articles


def fetch_all(queries=None, max_results=10):
    api_key = os.environ.get("GNEWSAPIKEY") or os.environ.get("GNEWS_API_KEY")
    if not api_key:
        print("  [warning] GNEWSAPIKEY not set — skipping GNews ingestion.")
        return [], {"fetched": 0, "matched": 0, "skipped": 0}

    queries = queries or NEWSAPI_QUERIES
    all_articles = []
    seen_urls = set()
    seen_titles = set()
    raw_total = 0
    skipped_dupes = 0

    for label, q in queries.items():
        print(f"Fetching GNews (IN): {label} ...")
        items = fetch_query(label, q, api_key, max_results=max_results)
        raw_total += len(items)
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
        print(f"  -> {kept} kept")

    stats = {
        "fetched": raw_total,
        "matched": len(all_articles),
        "skipped": skipped_dupes,
    }
    return all_articles, stats


if __name__ == "__main__":
    arts, stats = fetch_all()
    print(f"\nGNews: {stats['matched']} kept, {stats['skipped']} dupes skipped")
    for a in arts[:5]:
        print(f"- [{', '.join(a.sectors)}] {a.title}")
