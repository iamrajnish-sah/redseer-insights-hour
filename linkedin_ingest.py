"""
linkedin_ingest.py

Festive LinkedIn discovery for Flipkart / Amazon / Meesho / Myntra / Ajio.

Providers (first match wins unless LINKEDIN_PROVIDERS lists several):
  1. ScrapeGraphAI Search API  — SGAI_API_KEY  (fresh web search, past_week)
  2. Apify LinkedIn post search — APIFY_TOKEN   (real LinkedIn posts, postedLimit=week)
  3. Google News RSS fallback   — free          (when:Nd freshness filter)

Does NOT call GNews.io or NewsAPI — those quotas stay for the main news pipeline.

Stores matches as origin=linkedin_scrape with festive_sale sector.
"""

from __future__ import annotations

import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html import unescape

import feedparser
import requests

import database
from sector_keywords import make_summary

ORIGIN = "linkedin_scrape"

PLATFORMS = (
    "flipkart",
    "amazon",
    "meesho",
    "myntra",
    "ajio",
    "shopsy",
)

FESTIVE_TERMS = (
    "festive",
    "festival",
    "diwali",
    "dussehra",
    "navratri",
    "dhanteras",
    "big billion",
    "bbd",
    "great indian festival",
    "gif",
    "end of reason",
    "pink friday",
    "sale season",
    "shopping festival",
    "mega sale",
)

INSIGHT_TERMS = (
    "gmv",
    "gross merchandise",
    "orders",
    "aov",
    "average order",
    "conversion",
    "yoy",
    "year-on-year",
    "year over year",
    "market share",
    "category share",
    "growth",
    "crore",
    "cr ",
    "billion",
    "million",
    "percent",
    "%",
    "barometer",
    "outlook",
    "insight",
    "analysis",
    "trend",
    "traffic",
    "sessions",
    "units sold",
    "throughput",
    "take rate",
    "contribution",
)

LOW_SIGNAL = (
    "we're hiring",
    "we are hiring",
    "job opening",
    "congratulations to",
    "happy birthday",
    "office party",
    "team outing",
)

USER_AGENT = (
    "Mozilla/5.0 (compatible; RedseerInsightHour/1.0; +https://redseer.com)"
)

# Short LinkedIn-native style queries (Apify / ScrapeGraph) — fresher than news indexes.
LINKEDIN_SEARCH_QUERIES = (
    (
        "Flipkart BBD GMV",
        'Flipkart ("Big Billion" OR BBD) (GMV OR orders OR crore OR YoY)',
    ),
    (
        "Amazon GIF metrics",
        'Amazon ("Great Indian Festival" OR GIF OR Diwali) (GMV OR orders OR crore OR insight)',
    ),
    (
        "Meesho festive",
        'Meesho (festive OR Diwali) (GMV OR orders OR barometer OR crore OR growth)',
    ),
    (
        "Myntra festive",
        'Myntra (festive OR Diwali OR "Big Fashion") (GMV OR AOV OR orders OR crore)',
    ),
    (
        "Ajio festive",
        'Ajio (festive OR Diwali) (GMV OR orders OR crore OR growth OR insight)',
    ),
    (
        "Festive ecommerce barometer",
        '("festive barometer" OR "festive sale" OR "festive season") (Flipkart OR Amazon OR Meesho) (GMV OR ecommerce)',
    ),
)

# RSS fallback — Google News with when:Nd (still free; often stale → last resort).
LINKEDIN_FESTIVE_QUERIES = tuple(
    (label, f"site:linkedin.com {q}") for label, q in LINKEDIN_SEARCH_QUERIES
)


@dataclass
class Article:
    source: str
    pub_date: str
    title: str
    subtitle: str = ""
    byline: str = ""
    body: str = ""
    origin: str = ORIGIN
    url: str = None
    page: str = None
    article_id: str = None
    sectors: list = field(default_factory=list)
    pre_classified: bool = False
    auto_summary: str = ""
    image_url: str = None
    resolved_url: str = None
    scrape_meta: dict = field(default_factory=dict)


def max_age_days() -> int:
    return max(1, min(int(os.environ.get("LINKEDIN_MAX_AGE_DAYS", "14")), 60))


def _max_age_days() -> int:
    return max_age_days()


def _cutoff_date() -> date:
    return date.today() - timedelta(days=_max_age_days())


def _clean(text: str) -> str:
    return unescape(re.sub(r"\s+", " ", text or "")).strip()


def _hay(title: str, body: str = "") -> str:
    return f"{title or ''} {body or ''}".lower()


def _has_any(hay: str, terms) -> bool:
    return any(t in hay for t in terms)


def _extract_metrics(text: str) -> list:
    found = []
    patterns = [
        r"\b\d+(?:\.\d+)?\s*%",
        r"\b(?:Rs\.?|INR|₹)\s*[\d,]+(?:\.\d+)?\s*(?:cr|crore|lakh|bn|billion)?",
        r"\b\d+(?:\.\d+)?\s*(?:cr|crore|lakh|mn|million|bn|billion)\b",
        r"\b(?:GMV|AOV|YoY|orders?)\b[^.]{0,40}",
    ]
    for pat in patterns:
        for m in re.finditer(pat, text or "", flags=re.I):
            snippet = _clean(m.group(0))[:80]
            if snippet and snippet not in found:
                found.append(snippet)
            if len(found) >= 5:
                return found
    return found


def quality_score(title: str, body: str = "", *, relax: bool = False) -> dict:
    """Score LinkedIn candidates for festive data / insight quality."""
    hay = _hay(title, body)
    platforms = [p for p in PLATFORMS if p in hay]
    festive = _has_any(hay, FESTIVE_TERMS)
    insight = _has_any(hay, INSIGHT_TERMS)
    low = _has_any(hay, LOW_SIGNAL)
    metrics = _extract_metrics(f"{title} {body}")
    score = 0
    if platforms:
        score += 2 + min(2, len(platforms))
    if festive:
        score += 2
    if insight:
        score += 3
    if metrics:
        score += min(3, len(metrics))
    if "linkedin.com" in hay or "linkedin" in (title or "").lower():
        score += 1
    if low and not metrics:
        score -= 3
    if relax:
        # Real LinkedIn / ScrapeGraph hits: platform + festive is enough.
        keep = score >= 4 and bool(platforms) and festive
    else:
        keep = score >= 5 and bool(platforms) and festive and (insight or metrics)
    return {
        "score": score,
        "keep": keep,
        "platforms": platforms,
        "festive": festive,
        "insight": insight,
        "metrics": metrics,
        "low_signal": low,
    }


def _parse_iso_date(raw) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        ts = float(raw)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        except (OSError, OverflowError, ValueError):
            return None
    text = str(raw).strip()
    if not text:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def _is_fresh(pub_date: str) -> bool:
    if not pub_date or not re.match(r"\d{4}-\d{2}-\d{2}", pub_date):
        # Unknown date — keep only for paid providers (caller decides); RSS drops unknown as stale.
        return False
    try:
        return date.fromisoformat(pub_date[:10]) >= _cutoff_date()
    except ValueError:
        return False


def _looks_like_linkedin(url: str, title: str = "", summary: str = "") -> bool:
    hay = f"{url or ''} {title or ''} {summary or ''}".lower()
    return "linkedin.com" in hay or "linkedin" in hay


def _title_key(title: str) -> str:
    return re.sub(r"\W+", " ", (title or "").lower()).strip()[:90]


def _make_article(
    *,
    label: str,
    title: str,
    body: str,
    url: str,
    pub_date: str,
    discovery: str,
    byline: str = "",
    image_url: str = None,
    relax: bool = False,
    extra_meta: dict = None,
) -> Article | None:
    title = _clean(title)
    body = _clean(body)
    if not title:
        return None
    if not _is_fresh(pub_date):
        return None
    quality = quality_score(title, body, relax=relax)
    if not quality["keep"]:
        return None
    platform_label = ", ".join(p.title() for p in quality["platforms"]) or "Festive"
    metrics_line = "; ".join(quality["metrics"]) if quality["metrics"] else ""
    full_body = body
    if metrics_line and metrics_line.lower() not in body.lower():
        full_body = f"{body}\n\nData points: {metrics_line}".strip()
    meta = {
        "channel": "linkedin",
        "discovery": discovery,
        "query_label": label,
        "platforms": quality["platforms"],
        "insight_score": quality["score"],
        "data_points": quality["metrics"],
        "post_url": url,
        "fresh_within_days": _max_age_days(),
    }
    if extra_meta:
        meta.update(extra_meta)
    return Article(
        source=f"LinkedIn festive · {label}",
        pub_date=pub_date,
        title=title[:300],
        subtitle=f"{platform_label} · insight score {quality['score']} · {discovery}",
        byline=byline or "",
        body=full_body[:4000],
        origin=ORIGIN,
        url=url or None,
        resolved_url=url or None,
        page="linkedin",
        image_url=image_url,
        sectors=["festive_sale"],
        pre_classified=True,
        auto_summary=make_summary(title, full_body),
        scrape_meta=meta,
    )


# ---------------------------------------------------------------------------
# Provider: ScrapeGraphAI Search (SGAI_API_KEY)
# ---------------------------------------------------------------------------


def sgai_configured() -> bool:
    return bool((os.environ.get("SGAI_API_KEY") or os.environ.get("SCRAPEGRAPH_API_KEY") or "").strip())


def sgai_api_key() -> str:
    return (os.environ.get("SGAI_API_KEY") or os.environ.get("SCRAPEGRAPH_API_KEY") or "").strip()


def _sgai_search(query: str, num_results: int = 8) -> dict:
    """Call ScrapeGraphAI v2 Search API via HTTP (no heavy SDK required)."""
    key = sgai_api_key()
    if not key:
        raise RuntimeError("SGAI_API_KEY is not configured")
    base = (
        os.environ.get("SGAI_API_URL")
        or "https://v2-api.scrapegraphai.com/api"
    ).rstrip("/")
    time_range = os.environ.get("LINKEDIN_SGAI_TIME_RANGE", "past_week")
    payload = {
        "query": query[:500],
        "numResults": max(1, min(int(num_results), 12)),
        "timeRange": time_range,
        "locationGeoCode": os.environ.get("LINKEDIN_SGAI_GEO", "in"),
        "prompt": (
            "From these search hits, keep only recent LinkedIn posts or LinkedIn shares "
            "about Indian festive ecommerce (Flipkart BBD, Amazon Great Indian Festival, "
            "Meesho, Myntra, Ajio). Prefer posts with GMV, orders, AOV, YoY, crore, "
            "market share, festive barometer. Ignore hiring, memes, and generic ads. "
            "Return JSON with posts: [{title, url, snippet, published_date, author, platforms}]."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "posts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "url": {"type": "string"},
                            "snippet": {"type": "string"},
                            "published_date": {"type": "string"},
                            "author": {"type": "string"},
                            "platforms": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                    },
                }
            },
        },
    }
    timeout = float(os.environ.get("LINKEDIN_SGAI_TIMEOUT", "90"))
    resp = requests.post(
        f"{base}/search",
        headers={
            "SGAI-APIKEY": key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json=payload,
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"ScrapeGraphAI search failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json() if resp.content else {}


def _posts_from_sgai_payload(data: dict) -> list:
    """Normalize ScrapeGraphAI response shapes into post dicts."""
    posts = []
    if not isinstance(data, dict):
        return posts
    # v2 ApiResult-style or raw body
    body = data.get("data") if isinstance(data.get("data"), dict) else data
    json_data = body.get("json_data") or body.get("jsonData") or body.get("result") or {}
    if isinstance(json_data, dict):
        raw_posts = json_data.get("posts") or []
        if isinstance(raw_posts, list):
            posts.extend([p for p in raw_posts if isinstance(p, dict)])
    # Also harvest raw search hits (title/url/snippet)
    results = body.get("results") or data.get("results") or []
    if isinstance(results, list):
        for hit in results:
            if not isinstance(hit, dict):
                continue
            url = hit.get("url") or hit.get("link") or ""
            title = hit.get("title") or ""
            snippet = hit.get("snippet") or hit.get("description") or hit.get("content") or ""
            if _looks_like_linkedin(url, title, snippet):
                posts.append(
                    {
                        "title": title,
                        "url": url,
                        "snippet": snippet,
                        "published_date": hit.get("published_date")
                        or hit.get("date")
                        or date.today().isoformat(),
                        "author": hit.get("source") or hit.get("author") or "",
                    }
                )
    return posts


def fetch_via_scrapegraph(max_per_query: int = 6) -> tuple[list, list]:
    """Return (articles, errors) from ScrapeGraphAI Search."""
    if not sgai_configured():
        return [], ["ScrapeGraphAI: SGAI_API_KEY not set"]
    query_cap = int(os.environ.get("LINKEDIN_SGAI_QUERY_CAP", "3" if os.environ.get("VERCEL") else "5"))
    articles = []
    errors = []
    for label, query in LINKEDIN_SEARCH_QUERIES[:query_cap]:
        try:
            # Bias search toward LinkedIn URLs.
            q = f"site:linkedin.com {query}"
            raw = _sgai_search(q, num_results=max_per_query)
            for post in _posts_from_sgai_payload(raw):
                title = post.get("title") or ""
                snippet = post.get("snippet") or post.get("content") or ""
                url = (post.get("url") or "").strip()
                if not _looks_like_linkedin(url, title, snippet):
                    continue
                pub = _parse_iso_date(post.get("published_date") or post.get("date")) or date.today().isoformat()
                platforms = post.get("platforms") or []
                body = snippet
                if platforms:
                    body = f"{snippet}\nPlatforms: {', '.join(map(str, platforms))}".strip()
                art = _make_article(
                    label=label,
                    title=title,
                    body=body,
                    url=url,
                    pub_date=pub,
                    discovery="scrapegraphai",
                    byline=_clean(post.get("author") or ""),
                    relax=True,
                )
                if art:
                    articles.append(art)
        except Exception as exc:
            errors.append(f"ScrapeGraphAI · {label}: {str(exc)[:180]}")
    return articles, errors


# ---------------------------------------------------------------------------
# Provider: Apify LinkedIn post search (APIFY_TOKEN)
# ---------------------------------------------------------------------------


def apify_configured() -> bool:
    return bool((os.environ.get("APIFY_TOKEN") or os.environ.get("APIFY_API_TOKEN") or "").strip())


def _apify_token() -> str:
    return (os.environ.get("APIFY_TOKEN") or os.environ.get("APIFY_API_TOKEN") or "").strip()


def _apify_linkedin_actor() -> str:
    return (
        os.environ.get("APIFY_LINKEDIN_ACTOR")
        or "harvestapi~linkedin-post-search"
    ).strip()


def _apify_run_sync(actor_id: str, payload: dict, wait_secs: int = 120) -> list:
    token = _apify_token()
    if not token:
        raise RuntimeError("APIFY_TOKEN is not configured")
    actor_id = actor_id.replace("/", "~")
    start_url = (
        f"https://api.apify.com/v2/acts/{actor_id}/runs"
        f"?token={token}&waitForFinish={int(wait_secs)}"
    )
    resp = requests.post(start_url, json=payload, timeout=wait_secs + 30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Apify start failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json().get("data") or {}
    status = data.get("status")
    if status != "SUCCEEDED":
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


def _apify_item_to_article(item: dict, label: str) -> Article | None:
    if not isinstance(item, dict):
        return None
    # Skip nested reaction/comment rows
    if (item.get("type") or "").lower() in ("reaction", "comment"):
        return None
    content = _clean(
        item.get("content")
        or item.get("text")
        or item.get("commentary")
        or item.get("postText")
        or ""
    )
    url = (
        item.get("linkedinUrl")
        or item.get("url")
        or item.get("postUrl")
        or item.get("shareUrl")
        or ""
    ).strip()
    author = item.get("author") or {}
    author_name = ""
    if isinstance(author, dict):
        author_name = _clean(author.get("name") or author.get("publicIdentifier") or "")
    elif isinstance(author, str):
        author_name = _clean(author)
    posted = item.get("postedAt") or {}
    pub = None
    if isinstance(posted, dict):
        pub = _parse_iso_date(posted.get("date") or posted.get("timestamp"))
    if not pub:
        pub = _parse_iso_date(
            item.get("postedDate")
            or item.get("publishedAt")
            or item.get("date")
            or item.get("timestamp")
        )
    if not pub:
        # Fresh actor with postedLimit=week — treat missing date as today.
        pub = date.today().isoformat()
    title = content[:140] if content else _clean(item.get("title") or "")
    if author_name and title:
        # Prefer a readable card title.
        first_line = content.split(".")[0][:120] if content else title
        title = first_line or title
    image = None
    images = item.get("postImages") or item.get("images") or []
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict):
            image = first.get("url")
        elif isinstance(first, str):
            image = first
    engagement = item.get("engagement") or {}
    likes = engagement.get("likes") if isinstance(engagement, dict) else None
    art = _make_article(
        label=label,
        title=title or f"LinkedIn post · {author_name or 'festive'}",
        body=content or title,
        url=url,
        pub_date=pub,
        discovery="apify_linkedin",
        byline=author_name,
        image_url=image if isinstance(image, str) else None,
        relax=True,
        extra_meta={
            "author": author_name,
            "likes": likes,
            "comments": engagement.get("comments") if isinstance(engagement, dict) else None,
        },
    )
    return art


def fetch_via_apify(max_per_query: int = 8) -> tuple[list, list]:
    """Return (articles, errors) from Apify LinkedIn post search."""
    if not apify_configured():
        return [], ["Apify: APIFY_TOKEN not set"]
    actor = _apify_linkedin_actor()
    posted_limit = os.environ.get("LINKEDIN_APIFY_POSTED_LIMIT", "week")
    query_cap = int(os.environ.get("LINKEDIN_APIFY_QUERY_CAP", "3" if os.environ.get("VERCEL") else "5"))
    max_posts = max(1, min(int(max_per_query), 15))
    wait_secs = int(os.environ.get("LINKEDIN_APIFY_WAIT_SECS", "150"))
    articles = []
    errors = []
    # One Apify run with several queries is cheaper/faster than N runs.
    queries = [q for _, q in LINKEDIN_SEARCH_QUERIES[:query_cap]]
    labels = [label for label, _ in LINKEDIN_SEARCH_QUERIES[:query_cap]]
    try:
        payload = {
            "searchQueries": queries,
            "maxPosts": max_posts,
            "postedLimit": posted_limit,
            "sortBy": "date",
            "scrapeReactions": False,
            "scrapeComments": False,
        }
        items = _apify_run_sync(actor, payload, wait_secs=wait_secs)
        for item in items:
            # Tag with first matching label for display.
            content = _clean(
                (item or {}).get("content")
                or (item or {}).get("text")
                or ""
            ).lower()
            label = labels[0]
            for lab, q in zip(labels, queries):
                # crude match: platform token from label
                token = lab.split()[0].lower()
                if token in content:
                    label = lab
                    break
            art = _apify_item_to_article(item, label)
            if art:
                articles.append(art)
    except Exception as exc:
        errors.append(f"Apify LinkedIn: {str(exc)[:200]}")
    return articles, errors


# ---------------------------------------------------------------------------
# Provider: Google News RSS fallback (free)
# ---------------------------------------------------------------------------


def _google_news_rss_url(query: str) -> str:
    encoded = urllib.parse.quote(query)
    return f"https://news.google.com/rss/search?q={encoded}&hl=en-IN&gl=IN&ceid=IN:en"


def _entry_source_label(entry) -> str:
    src = entry.get("source")
    if isinstance(src, dict):
        return _clean(src.get("title") or "")
    if isinstance(src, str):
        return _clean(src)
    return ""


def _parse_pub_date(entry) -> str:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return date(*parsed[:3]).isoformat()
            except (TypeError, ValueError):
                pass
    raw = (entry.get("published") or entry.get("updated") or "")[:10]
    if re.match(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    return ""


def fetch_query_rss(label: str, query: str, max_items: int = 12):
    """Return (articles, error_message_or_None)."""
    when = os.environ.get("LINKEDIN_RSS_WHEN", f"when:{_max_age_days()}d")
    full_q = f"{query} {when}".strip()
    url = _google_news_rss_url(full_q)
    timeout = float(os.environ.get("LINKEDIN_RSS_TIMEOUT", "12"))
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except requests.RequestException as exc:
        return [], str(exc)[:200]

    articles = []
    for entry in (parsed.entries or [])[:max_items]:
        title = _clean(entry.get("title") or "")
        if not title:
            continue
        link = (entry.get("link") or "").strip()
        summary = _clean(entry.get("summary") or entry.get("description") or "")
        source_label = _entry_source_label(entry)
        if not _looks_like_linkedin(link, f"{title} {source_label}", summary):
            continue
        pub = _parse_pub_date(entry)
        display_title = re.sub(r"\s+[-–|]\s*LinkedIn\s*$", "", title, flags=re.I).strip() or title
        art = _make_article(
            label=label,
            title=display_title,
            body=summary,
            url=link,
            pub_date=pub,
            discovery="google_news_rss",
            relax=False,
            extra_meta={"publisher": source_label or "LinkedIn"},
        )
        if art:
            articles.append(art)
    return articles, None


def fetch_via_rss(max_per_query: int = 10) -> tuple[list, list]:
    query_cap = int(os.environ.get("LINKEDIN_QUERY_CAP", "4" if os.environ.get("VERCEL") else "6"))
    articles = []
    errors = []
    for label, query in LINKEDIN_FESTIVE_QUERIES[:query_cap]:
        batch, err = fetch_query_rss(label, query, max_items=max_per_query)
        if err:
            errors.append(f"RSS · {label}: {err}")
            continue
        articles.extend(batch)
    return articles, errors


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _provider_order() -> list:
    """
    Default: scrapegraph → apify → rss (only configured providers run).
    Override with LINKEDIN_PROVIDERS=scrapegraph,apify,rss
    """
    raw = (os.environ.get("LINKEDIN_PROVIDERS") or "").strip().lower()
    if raw:
        order = [p.strip() for p in raw.split(",") if p.strip()]
    else:
        order = ["scrapegraph", "apify", "rss"]
    # Skip unconfigured providers unless explicitly forced.
    enabled = []
    for name in order:
        if name in ("scrapegraph", "sgai", "scrapegraphai"):
            if sgai_configured() or (raw and name in raw):
                enabled.append("scrapegraph")
        elif name == "apify":
            if apify_configured() or (raw and "apify" in raw):
                enabled.append("apify")
        elif name == "rss":
            enabled.append("rss")
    # If nothing configured, always keep free RSS so the button still works.
    if not enabled:
        enabled = ["rss"]
    # Deduplicate while preserving order
    seen = set()
    out = []
    for p in enabled:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def fetch_festive_linkedin(max_per_query: int = None) -> dict:
    """Discover recent festive LinkedIn insight posts via best available provider(s)."""
    max_per_query = int(max_per_query or os.environ.get("LINKEDIN_MAX_PER_QUERY", "8"))
    providers = _provider_order()
    # If scrapegraph or apify succeeds with enough posts, skip stale RSS unless forced.
    force_all = os.environ.get("LINKEDIN_RUN_ALL_PROVIDERS", "").lower() in (
        "1",
        "true",
        "yes",
    )
    min_before_skip_rss = int(os.environ.get("LINKEDIN_MIN_BEFORE_SKIP_RSS", "3"))

    collected = []
    errors = []
    used = []
    seen = set()

    def _merge(batch, provider_name):
        added = 0
        for article in batch:
            key = _title_key(article.title)
            url_key = (article.url or "").rstrip("/").lower()
            dedupe = key or url_key
            if not dedupe or dedupe in seen:
                continue
            seen.add(dedupe)
            if url_key:
                seen.add(url_key)
            collected.append(article)
            added += 1
        if added:
            used.append(provider_name)
        return added

    for provider in providers:
        if provider == "scrapegraph":
            batch, errs = fetch_via_scrapegraph(max_per_query=max_per_query)
            errors.extend(errs)
            _merge(batch, "scrapegraphai")
        elif provider == "apify":
            batch, errs = fetch_via_apify(max_per_query=max_per_query)
            errors.extend(errs)
            _merge(batch, "apify_linkedin")
        elif provider == "rss":
            if (
                not force_all
                and any(p in used for p in ("scrapegraphai", "apify_linkedin"))
                and len(collected) >= min_before_skip_rss
            ):
                continue
            batch, errs = fetch_via_rss(max_per_query=max_per_query)
            errors.extend(errs)
            _merge(batch, "google_news_rss")

    collected.sort(
        key=lambda a: (
            a.pub_date or "",
            int((a.scrape_meta or {}).get("insight_score") or 0),
        ),
        reverse=True,
    )
    hard_cap = int(os.environ.get("LINKEDIN_RESULT_CAP", "40"))
    collected = collected[:hard_cap]

    inserted, ids, refreshed = database.insert_articles(collected)
    providers_note = ", ".join(used) if used else "none"
    return {
        "ok": True,
        "origin": ORIGIN,
        "providers_tried": providers,
        "providers_used": used,
        "max_age_days": _max_age_days(),
        "fetched": len(collected),
        "inserted": inserted,
        "refreshed": len(refreshed),
        "ids": ids,
        "errors": errors,
        "message": (
            f"LinkedIn festive discovery — {len(collected)} fresh insight posts "
            f"(≤{_max_age_days()}d) via {providers_note} "
            f"({inserted} new, {len(refreshed)} updated). "
            "Shown in LinkedIn box + Festive Sale tab. NewsAPI/GNews untouched."
        ),
        "terminology": {
            "platforms": list(PLATFORMS),
            "festive_terms": list(FESTIVE_TERMS[:12]),
            "insight_terms": list(INSIGHT_TERMS[:16]),
        },
        "setup_hint": _setup_hint(providers, used, errors),
    }


def _setup_hint(tried, used, errors) -> str:
    if "scrapegraphai" in used or "apify_linkedin" in used:
        return ""
    hints = []
    if not sgai_configured():
        hints.append("Set SGAI_API_KEY for ScrapeGraphAI fresh LinkedIn search")
    if not apify_configured():
        hints.append("or reuse APIFY_TOKEN with harvestapi/linkedin-post-search")
    if errors:
        hints.append("check provider errors in API response")
    return " · ".join(hints)


def status_summary() -> dict:
    providers = _provider_order()
    return {
        "origin": ORIGIN,
        "method": "+".join(providers),
        "providers": providers,
        "scrapegraph_configured": sgai_configured(),
        "apify_configured": apify_configured(),
        "uses_gnews_api": False,
        "uses_newsapi": False,
        "max_age_days": _max_age_days(),
        "query_count": len(LINKEDIN_SEARCH_QUERIES),
        "platforms": list(PLATFORMS),
        "note": (
            "Prefers ScrapeGraphAI Search (SGAI_API_KEY) and/or Apify LinkedIn post search "
            f"(APIFY_TOKEN, postedLimit=week). Falls back to Google News RSS with when:{_max_age_days()}d. "
            "Never uses GNews.io or NewsAPI quotas."
        ),
    }
