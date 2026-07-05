"""
newsapi_ingest.py

Pulls sector-targeted articles from NewsAPI.org. Results are tagged immediately
(keyword + query sector) — no Gemini needed, same as RSS.

Requires: NEWSAPIKEY environment variable (https://newsapi.org)
Free tier: 100 requests/day, articles delayed ~24h.
"""

import os
import requests
from dataclasses import dataclass, field
from datetime import date

from sector_keywords import NEWSAPI_QUERIES, match_sectors, make_summary
from dedupe import normalize_title

NEWSAPI_URL = "https://newsapi.org/v2/everything"


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


def fetch_query(query_label, query, api_key, page_size=20, language="en"):
    params = {
        "q": query,
        "language": language,
        "sortBy": "publishedAt",
        "pageSize": page_size,
        "apiKey": api_key,
    }
    resp = requests.get(NEWSAPI_URL, params=params, timeout=15)
    if resp.status_code != 200:
        print(f"  [warning] NewsAPI error for '{query_label}': {resp.status_code} {resp.text[:200]}")
        return []

    articles = []
    for item in resp.json().get("articles", []):
        title = (item.get("title") or "").strip()
        if not title or title == "[Removed]":
            continue
        pub_date = (item.get("publishedAt") or str(date.today()))[:10]
        article = Article(
            source=f"{item.get('source', {}).get('name', 'Unknown')} (NewsAPI: {query_label})",
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


def fetch_all(queries=None, page_size=20):
    api_key = os.environ.get("NEWSAPIKEY")
    if not api_key:
        print("  [warning] NEWSAPIKEY not set — skipping NewsAPI ingestion.")
        return [], {"fetched": 0, "matched": 0, "skipped": 0}

    queries = queries or NEWSAPI_QUERIES
    all_articles = []
    seen_urls = set()
    seen_titles = set()
    raw_total = 0
    skipped_dupes = 0

    for label, q in queries.items():
        print(f"Fetching NewsAPI: {label} ...")
        items = fetch_query(label, q, api_key, page_size)
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
    print(f"\nNewsAPI: {stats['matched']} kept, {stats['skipped']} URL dupes skipped")
    for a in arts[:5]:
        print(f"- [{', '.join(a.sectors)}] {a.title}")
