"""
newsapi_ingest.py

Pulls sector-targeted articles from NewsAPI.org. Results are tagged immediately
(keyword + query sector) — no Gemini needed.

Requires: NEWSAPIKEY environment variable (https://newsapi.org)

Production (Vercel): /everything is blocked off localhost, so we use
  /everything?domains=<Indian publishers> per sector (works on free tier).
  Fallback: top-headlines?sources=the-times-of-india,the-hindu
  — country=in always returns 0 on NewsAPI.

Local dev: full /everything sector OR queries (~300 articles).
"""

import os
import re
import time
import requests
from dataclasses import dataclass, field
from datetime import date, timedelta

from sector_keywords import (
    NEWSAPI_QUERIES,
    is_india_relevant,
    match_sectors,
    make_summary,
)
from sector_taxonomies import TAXONOMIES
from dedupe import normalize_title, story_cluster_key

NEWSAPI_EVERYTHING_URL = "https://newsapi.org/v2/everything"
NEWSAPI_TOP_URL = "https://newsapi.org/v2/top-headlines"

# country=in is empty on NewsAPI — these source IDs do return Indian headlines.
DEFAULT_INDIA_SOURCES = "the-times-of-india,the-hindu"


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
    return int(os.environ.get("NEWSAPI_PAGE_SIZE", "100"))


def _use_headlines():
    if os.environ.get("NEWSAPI_USE_HEADLINES", "").lower() in ("0", "false", "no"):
        return False
    if os.environ.get("NEWSAPI_USE_HEADLINES", "").lower() in ("1", "true", "yes"):
        return True
    return bool(os.environ.get("VERCEL"))


def _india_sources():
    raw = os.environ.get("NEWSAPI_INDIA_SOURCES", DEFAULT_INDIA_SOURCES)
    return [part.strip() for part in raw.split(",") if part.strip()]


def _normalize_url(url):
    if not url:
        return None
    return url.strip().rstrip("/") or url.strip()


def _headline_search_terms(query, max_terms=3):
    q = (query or "").strip()
    if " when:" in q:
        q = q.split(" when:")[0].strip()
    q = q.strip("()")
    if re.search(r"\s+OR\s+", q, re.I):
        terms = []
        for part in re.split(r"\s+OR\s+", q, flags=re.I):
            part = part.strip().strip('"').strip()
            if not part or part.lower() in ("india", "indian"):
                continue
            terms.append(part[:100])
            if len(terms) >= max_terms:
                break
        return terms or [q[:100]]
    return [q[:100]] if q else []


def _prepare_article_loose(article):
    sectors = match_sectors(article.title, article.body, article.subtitle)
    if not sectors:
        return None
    article.sectors = sectors
    article.pre_classified = True
    article.auto_summary = make_summary(article.title, article.body or article.subtitle)
    return article


def _prepare_article(article, query_sector):
    sectors = match_sectors(article.title, article.body, article.subtitle)
    if query_sector == "ride_hailing":
        if "ride_hailing" not in sectors:
            return None
    elif query_sector in TAXONOMIES:
        # Taxonomy sectors (e.g. value_commerce) are never force-tagged:
        # the semantic classifier decides. Keep only if some sector matched.
        if not sectors:
            return None
    elif query_sector not in sectors:
        sectors.insert(0, query_sector)
    article.sectors = sectors
    article.pre_classified = True
    article.auto_summary = make_summary(article.title, article.body or article.subtitle)
    return article


def _item_to_article(item, source_label):
    title = (item.get("title") or "").strip()
    if not title or title == "[Removed]" or title.lower() == "google news":
        return None
    pub_date = (item.get("publishedAt") or str(date.today()))[:10]
    publisher = (item.get("source") or {}).get("name") or "Unknown"
    return Article(
        source=f"{publisher} (NewsAPI: {source_label})",
        pub_date=pub_date,
        title=title,
        subtitle=item.get("description") or "",
        byline=item.get("author") or "",
        body=item.get("content") or item.get("description") or "",
        url=item.get("url"),
        image_url=item.get("urlToImage") or None,
    )


def _request_top_headlines(params):
    resp = requests.get(NEWSAPI_TOP_URL, params=params, timeout=20)
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code} — {resp.text[:200]}"
    payload = resp.json()
    if payload.get("status") == "error":
        return None, payload.get("message") or "NewsAPI error"
    return payload, None


def fetch_india_source_headlines(api_key, page_size=100):
    """Fetch from Indian publisher source IDs (country=in does not work on NewsAPI)."""
    sources = ",".join(_india_sources())
    params = {
        "sources": sources,
        "pageSize": min(page_size, 100),
        "apiKey": api_key,
    }
    payload, err = _request_top_headlines(params)
    if err:
        return [], 0, f"India sources: {err}"

    raw = payload.get("articles") or []
    articles = []
    for item in raw:
        article = _item_to_article(item, "India sources")
        if not article:
            continue
        prepared = _prepare_article_loose(article)
        if prepared:
            articles.append(prepared)
    return articles, len(raw), None


def fetch_global_category_headlines(category, api_key, page_size=100):
    """Global business/tech headlines — keep India-relevant stories only."""
    params = {
        "category": category,
        "pageSize": min(page_size, 100),
        "apiKey": api_key,
    }
    payload, err = _request_top_headlines(params)
    if err:
        return [], 0, f"{category}: {err}"

    raw = payload.get("articles") or []
    articles = []
    for item in raw:
        article = _item_to_article(item, f"global {category}")
        if not article:
            continue
        if not is_india_relevant(
            article.title, article.body, article.url, article.url
        ):
            continue
        prepared = _prepare_article_loose(article)
        if prepared:
            articles.append(prepared)
    return articles, len(raw), None


INDIA_NEWS_DOMAINS = (
    "livemint.com,economictimes.indiatimes.com,financialexpress.com,"
    "business-standard.com,moneycontrol.com,inc42.com,yourstory.com,"
    "hindustantimes.com,thehindu.com,timesofindia.indiatimes.com"
)


def _everything_blocked(message):
    if not message:
        return False
    lowered = message.lower()
    return any(
        token in lowered
        for token in (
            "localhost",
            "developer",
            "not authorized",
            "restricted",
            "upgrade",
            "availability",
        )
    )


def fetch_query_everything_domains(query_label, query, api_key, page_size=25):
    """Indian publisher domains — allowed on Vercel when broad /everything is not."""
    from_date = (date.today() - timedelta(days=int(os.environ.get("NEWSAPI_DAYS_BACK", "7")))).isoformat()
    domains = os.environ.get("NEWSAPI_INDIA_DOMAINS", INDIA_NEWS_DOMAINS)
    max_terms = int(os.environ.get("NEWSAPI_DOMAIN_TERMS", "3"))
    articles = []
    seen_urls = set()
    seen_titles = set()
    seen_clusters = set()
    raw_total = 0
    last_error = None

    for index, term in enumerate(_headline_search_terms(query, max_terms=max_terms)):
        if index > 0:
            time.sleep(float(os.environ.get("NEWSAPI_REQUEST_DELAY", "0.6")))
        search_q = term if "india" in term.lower() else f"{term} India"
        params = {
            "q": search_q,
            "domains": domains,
            "language": "en",
            "sortBy": "publishedAt",
            "from": from_date,
            "pageSize": min(page_size, 100),
            "apiKey": api_key,
        }
        resp = requests.get(NEWSAPI_EVERYTHING_URL, params=params, timeout=20)
        if resp.status_code != 200:
            last_error = f"{query_label}: HTTP {resp.status_code} — {resp.text[:200]}"
            continue

        payload = resp.json()
        if payload.get("status") == "error":
            last_error = f"{query_label}: {payload.get('message') or 'NewsAPI error'}"
            if _everything_blocked(last_error):
                return [], raw_total, last_error
            continue

        raw = payload.get("articles") or []
        raw_total += len(raw)
        sector_items = []
        for item in raw:
            article = _item_to_article(item, query_label)
            if not article:
                continue
            prepared = _prepare_article(article, query_label)
            if prepared:
                sector_items.append(prepared)
        _dedupe_into(articles, seen_urls, seen_titles, sector_items, seen_clusters)

    if not articles and last_error:
        return [], raw_total, last_error
    return articles, raw_total, None


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
    resp = requests.get(NEWSAPI_EVERYTHING_URL, params=params, timeout=20)
    if resp.status_code != 200:
        msg = resp.text[:200]
        return [], 0, f"{query_label}: HTTP {resp.status_code} — {msg}"

    payload = resp.json()
    if payload.get("status") == "error":
        err = payload.get("message") or "NewsAPI error"
        return [], 0, f"{query_label}: {err}"

    raw = payload.get("articles") or []
    articles = []
    for item in raw:
        article = _item_to_article(item, query_label)
        if not article:
            continue
        prepared = _prepare_article(article, query_label)
        if prepared:
            articles.append(prepared)
    return articles, len(raw), None


def _dedupe_into(all_articles, seen_urls, seen_titles, items, seen_clusters=None):
    kept = 0
    skipped = 0
    if seen_clusters is None:
        seen_clusters = set()
    for article in items:
        url = _normalize_url(article.url)
        title_key = normalize_title(article.title)
        cluster = story_cluster_key(article.title)
        if url and url in seen_urls:
            skipped += 1
            continue
        if title_key and title_key in seen_titles:
            skipped += 1
            continue
        if cluster and cluster in seen_clusters:
            skipped += 1
            continue
        if url:
            seen_urls.add(url)
        if title_key:
            seen_titles.add(title_key)
        if cluster:
            seen_clusters.add(cluster)
        all_articles.append(article)
        kept += 1
    return kept, skipped


def _fetch_sector_queries(api_key, page_size, fetch_fn, label_prefix):
    all_articles = []
    seen_urls = set()
    seen_titles = set()
    seen_clusters = set()
    raw_from_api = 0
    skipped_dupes = 0
    errors = []
    blocked = False

    started = time.monotonic()
    budget = float(
        os.environ.get(
            "NEWSAPI_BUDGET_SECONDS",
            "25" if os.environ.get("VERCEL") else "90",
        )
    )

    for index, (label, q) in enumerate(NEWSAPI_QUERIES.items()):
        if time.monotonic() - started > budget:
            errors.append(
                f"{label}: remaining sector queries skipped (time budget {int(budget)}s)"
            )
            break
        if index > 0:
            time.sleep(float(os.environ.get("NEWSAPI_REQUEST_DELAY", "0.6")))
        print(f"Fetching NewsAPI ({label_prefix}): {label} ...")
        items, raw_count, err = fetch_fn(label, q, api_key, page_size)
        raw_from_api += raw_count
        if err:
            errors.append(err)
            if _everything_blocked(err):
                blocked = True
                break
        kept, skipped = _dedupe_into(
            all_articles, seen_urls, seen_titles, items, seen_clusters
        )
        skipped_dupes += skipped
        print(f"  -> {kept} kept ({raw_count} from API)")

    return all_articles, raw_from_api, skipped_dupes, errors, blocked


def fetch_all_headlines_production(api_key, page_size):
    """Vercel-safe ingestion: Indian sources + India-filtered global categories."""
    all_articles = []
    seen_urls = set()
    seen_titles = set()
    seen_clusters = set()
    raw_from_api = 0
    skipped_dupes = 0
    errors = []

    print("Fetching NewsAPI (top-headlines): India sources ...")
    items, raw_count, err = fetch_india_source_headlines(api_key, page_size)
    raw_from_api += raw_count
    if err:
        errors.append(err)
    kept, skipped = _dedupe_into(
        all_articles, seen_urls, seen_titles, items, seen_clusters
    )
    skipped_dupes += skipped
    print(f"  -> {kept} kept ({raw_count} from API)")

    for category in ("business", "technology"):
        time.sleep(float(os.environ.get("NEWSAPI_REQUEST_DELAY", "0.6")))
        print(f"Fetching NewsAPI (top-headlines): global {category} (India filter) ...")
        items, raw_count, err = fetch_global_category_headlines(category, api_key, page_size)
        raw_from_api += raw_count
        if err:
            errors.append(err)
        kept, skipped = _dedupe_into(
            all_articles, seen_urls, seen_titles, items, seen_clusters
        )
        skipped_dupes += skipped
        print(f"  -> {kept} kept ({raw_count} from API)")

    return all_articles, raw_from_api, skipped_dupes, errors, "top-headlines-india"


def _fetch_all_result(articles, raw, skipped, errors, endpoint):
    return articles, {
        "fetched": len(articles),
        "raw_from_api": raw,
        "matched": len(articles),
        "skipped": skipped,
        "errors": errors,
        "endpoint": endpoint,
    }


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

    page_size = page_size or _page_size()
    production = _use_headlines()
    raw_total = 0
    skipped_total = 0
    errors = []
    blocked = False

    # On Vercel, broad /everything is blocked — Indian-domain queries work and are tried first.
    fetch_order = (
        [
            (fetch_query_everything_domains, "everything-domains"),
            (fetch_query_everything, "everything"),
        ]
        if production
        else [
            (fetch_query_everything, "everything"),
            (fetch_query_everything_domains, "everything-domains"),
        ]
    )

    articles = []
    endpoint = "none"
    for fetch_fn, label in fetch_order:
        if articles:
            break
        print(f"  [info] NewsAPI strategy: {label} ...")
        batch, raw, skipped, batch_errors, batch_blocked = _fetch_sector_queries(
            api_key, page_size, fetch_fn, label
        )
        raw_total += raw
        skipped_total += skipped
        errors.extend(batch_errors)
        blocked = blocked or batch_blocked
        if batch:
            articles = batch
            endpoint = label

    if articles:
        return _fetch_all_result(articles, raw_total, skipped_total, errors, endpoint)

    # Last resort: Indian publisher source IDs (country=in returns 0 on NewsAPI).
    print("  [info] using NewsAPI top-headlines from Indian publisher sources ...")
    articles, raw3, skipped3, errors3, endpoint = fetch_all_headlines_production(
        api_key, page_size
    )
    errors.extend(errors3)
    return _fetch_all_result(
        articles,
        raw_total + raw3,
        skipped_total + skipped3,
        errors,
        endpoint,
    )


if __name__ == "__main__":
    arts, stats = fetch_all()
    print(f"\nNewsAPI ({stats['endpoint']}): {stats['matched']} kept, {stats['raw_from_api']} raw")
    for a in arts[:5]:
        print(f"- [{', '.join(a.sectors)}] {a.title}")
