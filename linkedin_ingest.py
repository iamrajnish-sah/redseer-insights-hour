"""
linkedin_ingest.py

Festive LinkedIn *discovery* (not fixed-account scraping).

Uses free Google News RSS queries scoped to site:linkedin.com for
Flipkart / Amazon / Meesho / Myntra / Ajio festive sale insight posts.

Does NOT call GNews.io or NewsAPI — those quotas stay for the main news pipeline.
Does NOT scrape LinkedIn HTML login walls.

Stores matches as origin=linkedin_scrape with festive_sale sector so they:
  - appear in the dedicated LinkedIn scrape box
  - feed Festive Sale tab + intelligence briefs (like website scrape)
"""

from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime
from html import unescape

import feedparser
import requests

import database
from sector_keywords import make_summary

ORIGIN = "linkedin_scrape"

# Platforms we care about for festive monitoring.
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

# Prefer posts that look like insight cards / barometers, not generic promos.
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

# Soft-negative: hiring / meme / pure brand creative without numbers.
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

# Curated search queries — terminology tuned for data-rich festive LinkedIn posts.
LINKEDIN_FESTIVE_QUERIES = (
    (
        "Flipkart BBD GMV / orders",
        'site:linkedin.com Flipkart ("Big Billion" OR BBD OR festive) (GMV OR orders OR crore OR YoY OR "market share")',
    ),
    (
        "Amazon GIF festive metrics",
        'site:linkedin.com Amazon ("Great Indian Festival" OR GIF OR festive OR Diwali) (GMV OR orders OR crore OR growth OR insight)',
    ),
    (
        "Meesho festive barometer",
        'site:linkedin.com Meesho (festive OR Diwali OR "sale season") (GMV OR orders OR barometer OR outlook OR crore OR growth)',
    ),
    (
        "Myntra festive fashion",
        'site:linkedin.com Myntra (festive OR Diwali OR "Big Fashion") (GMV OR orders OR AOV OR crore OR category OR growth)',
    ),
    (
        "Ajio festive / Reliance",
        'site:linkedin.com Ajio (festive OR Diwali OR sale) (GMV OR orders OR crore OR growth OR insight OR Reliance)',
    ),
    (
        "Cross-platform festive e-retail",
        'site:linkedin.com (Flipkart OR Amazon OR Meesho OR Myntra OR Ajio) ("festive sale" OR "festive season" OR "festive barometer") (GMV OR "e-retail" OR ecommerce OR "online retail")',
    ),
    (
        "India festive ecommerce insights",
        'site:linkedin.com ("festive barometer" OR "festive outlook" OR "how India shops") (Flipkart OR Amazon OR Meesho OR ecommerce)',
    ),
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


def _google_news_rss_url(query: str) -> str:
    encoded = urllib.parse.quote(query)
    return f"https://news.google.com/rss/search?q={encoded}&hl=en-IN&gl=IN&ceid=IN:en"


def _clean(text: str) -> str:
    return unescape(re.sub(r"\s+", " ", text or "")).strip()


def _hay(title: str, body: str = "") -> str:
    return f"{title or ''} {body or ''}".lower()


def _has_any(hay: str, terms) -> bool:
    return any(t in hay for t in terms)


def _extract_metrics(text: str) -> list:
    """Pull rough metric phrases for the LinkedIn card / Excel-ish meta."""
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


def quality_score(title: str, body: str = "") -> dict:
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
    # Need at least one platform + festive signal, and insight/data lean.
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
    return date.today().isoformat()


def _looks_like_linkedin(url: str, title: str = "", summary: str = "") -> bool:
    hay = f"{url or ''} {title or ''} {summary or ''}".lower()
    return "linkedin.com" in hay or "linkedin" in hay


def _entry_source_label(entry) -> str:
    src = entry.get("source")
    if isinstance(src, dict):
        return _clean(src.get("title") or "")
    if isinstance(src, str):
        return _clean(src)
    return ""


def fetch_query_rss(label: str, query: str, max_items: int = 12):
    """Return (articles, error_message_or_None)."""
    url = _google_news_rss_url(query)
    timeout = float(os.environ.get("LINKEDIN_RSS_TIMEOUT", "12"))
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml, */*"},
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
        # Google News links are usually news.google.com redirects — use title/source too.
        if not _looks_like_linkedin(link, f"{title} {source_label}", summary):
            continue
        quality = quality_score(title, f"{summary} {source_label}")
        if not quality["keep"]:
            continue
        platform_label = ", ".join(p.title() for p in quality["platforms"]) or "Festive"
        metrics_line = "; ".join(quality["metrics"]) if quality["metrics"] else ""
        body = summary
        if metrics_line:
            body = f"{summary}\n\nData points: {metrics_line}".strip()
        display_title = title
        # Strip trailing " - LinkedIn" publisher suffix from Google News titles.
        display_title = re.sub(r"\s+[-–|]\s*LinkedIn\s*$", "", display_title, flags=re.I).strip() or title
        articles.append(
            Article(
                source=f"LinkedIn festive · {label}",
                pub_date=_parse_pub_date(entry),
                title=display_title[:300],
                subtitle=f"{platform_label} · insight score {quality['score']}",
                body=body[:4000],
                origin=ORIGIN,
                url=link or None,
                resolved_url=link or None,
                page="linkedin",
                sectors=["festive_sale"],
                pre_classified=True,
                auto_summary=make_summary(display_title, body),
                scrape_meta={
                    "channel": "linkedin",
                    "discovery": "google_news_rss",
                    "query_label": label,
                    "platforms": quality["platforms"],
                    "insight_score": quality["score"],
                    "data_points": quality["metrics"],
                    "post_url": link,
                    "publisher": source_label or "LinkedIn",
                },
            )
        )
    return articles, None


def fetch_festive_linkedin(max_per_query: int = None) -> dict:
    """Run curated LinkedIn festive discovery queries (free Google News RSS)."""
    max_per_query = int(
        max_per_query
        or os.environ.get("LINKEDIN_MAX_PER_QUERY", "10")
    )
    # Cap total queries on Vercel to stay under timeout.
    query_cap = int(os.environ.get("LINKEDIN_QUERY_CAP", "5" if os.environ.get("VERCEL") else "7"))
    queries = LINKEDIN_FESTIVE_QUERIES[:query_cap]

    collected = []
    errors = []
    seen_titles = set()
    for label, query in queries:
        batch, err = fetch_query_rss(label, query, max_items=max_per_query)
        if err:
            errors.append(f"{label}: {err}")
            continue
        for article in batch:
            key = re.sub(r"\W+", " ", (article.title or "").lower()).strip()[:90]
            if key in seen_titles:
                continue
            seen_titles.add(key)
            collected.append(article)

    # Highest insight score first before insert.
    collected.sort(
        key=lambda a: int((a.scrape_meta or {}).get("insight_score") or 0),
        reverse=True,
    )
    hard_cap = int(os.environ.get("LINKEDIN_RESULT_CAP", "40"))
    collected = collected[:hard_cap]

    inserted, ids, refreshed = database.insert_articles(collected)
    return {
        "ok": True,
        "origin": ORIGIN,
        "queries_run": len(queries),
        "fetched": len(collected),
        "inserted": inserted,
        "refreshed": len(refreshed),
        "ids": ids,
        "errors": errors,
        "message": (
            f"LinkedIn festive discovery — {len(collected)} insight posts "
            f"({inserted} new, {len(refreshed)} updated) from {len(queries)} search queries. "
            "Shown in LinkedIn box + Festive Sale tab."
        ),
        "terminology": {
            "platforms": list(PLATFORMS),
            "festive_terms": list(FESTIVE_TERMS[:12]),
            "insight_terms": list(INSIGHT_TERMS[:16]),
        },
    }


def status_summary() -> dict:
    return {
        "origin": ORIGIN,
        "method": "google_news_rss_site_linkedin",
        "uses_gnews_api": False,
        "uses_apify": False,
        "query_count": len(LINKEDIN_FESTIVE_QUERIES),
        "platforms": list(PLATFORMS),
        "note": (
            "Free discovery via Google News RSS (site:linkedin.com). "
            "Does not use GNews/NewsAPI quotas or Apify."
        ),
    }
