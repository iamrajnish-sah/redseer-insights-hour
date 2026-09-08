"""
dedupe.py

Finds likely-duplicate articles (same story, different URL/source).

Layers:
  1. Exact URL
  2. Exact normalized title + publish date
  3. Same-story clustering (Amazon hiring 1.6 lakh from 6 publishers, etc.)
"""

import os
import re
from datetime import datetime

import database


SOURCE_SUFFIX_RE = re.compile(
    r"\s+[-–—|]\s+[A-Za-z0-9][A-Za-z0-9 .,&'/]{1,50}$"
)
LAKH_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*lakh\b", re.I)
CRORE_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*crore\b", re.I)
INDIAN_COMMA_NUM_RE = re.compile(r"\b(\d{1,2}(?:,\d{2}){2,})\b")
PLAIN_NUM_RE = re.compile(r"\b(\d{5,})\b")

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "at", "by",
    "with", "from", "after", "ahead", "over", "as", "is", "its", "new", "says",
    "said", "report", "reports", "news", "india", "indian", "will", "than",
    "this", "that", "into", "vs", "via", "amid", "how", "why", "what",
}

THEME_TOKENS = {
    "festive", "festival", "diwali", "seasonal", "hiring", "hire", "jobs",
    "job", "workers", "workforce", "opportunities", "ipo", "funding", "gst",
    "strike", "layoff", "layoffs", "acquisition", "merger",
}

KNOWN_ORGS = {
    "amazon", "flipkart", "myntra", "meesho", "nykaa", "ajio", "jiomart",
    "shopsy", "snapdeal", "ondc", "swiggy", "zomato", "blinkit", "zepto",
    "instamart", "bigbasket", "dunzo", "ola", "uber", "rapido", "blusmart",
    "paytm", "phonepe", "razorpay", "bharatpe", "delhivery", "shadowfax",
    "shiprocket", "cadbury", "mondelez", "nestle", "ferrero", "amul",
    "kuku", "kukutv", "storytv", "pratilipi", "jiosaavn", "gaana",
    "hotstar", "jiocinema", "zee5", "netflix", "samsung", "apple",
    "walmart", "google", "meta", "microsoft", "reliance", "tata",
}

SYNONYMS = {
    "jobs": "jobs", "job": "jobs", "workers": "jobs", "worker": "jobs",
    "workforce": "jobs", "hiring": "jobs", "hire": "jobs",
    "opportunities": "jobs", "opportunity": "jobs", "work": "jobs",
    "festive": "festive", "festival": "festive",
}


def normalize_title(title):
    """Stable key for headline matching across sources."""
    text = (title or "").lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def strip_source_suffix(title):
    """Drop trailing publisher names from Google News headlines."""
    text = (title or "").strip()
    return SOURCE_SUFFIX_RE.sub("", text).strip() or text


def _expand_scale_numbers(text):
    def lakh(match):
        return str(int(round(float(match.group(1)) * 100000)))

    def crore(match):
        return str(int(round(float(match.group(1)) * 10000000)))

    text = LAKH_RE.sub(lakh, text)
    text = CRORE_RE.sub(crore, text)
    return text


def _extract_numbers(text):
    numbers = []
    for match in INDIAN_COMMA_NUM_RE.finditer(text):
        numbers.append(int(match.group(1).replace(",", "")))
    for match in PLAIN_NUM_RE.finditer(text):
        numbers.append(int(match.group(1)))
    # de-dupe, keep order
    seen = set()
    unique = []
    for num in numbers:
        if num not in seen:
            seen.add(num)
            unique.append(num)
    return unique


def _tokens(title):
    stripped = strip_source_suffix(title)
    expanded = _expand_scale_numbers(stripped.lower())
    expanded = expanded.replace(",", "")
    return re.findall(r"[a-z0-9]+", expanded)


def _primary_org(tokens):
    for token in tokens:
        if token in KNOWN_ORGS:
            return token
    for token in tokens:
        if token in STOPWORDS or token.isdigit() or len(token) < 4:
            continue
        return token
    return None


def _themes(tokens):
    themes = []
    for token in tokens:
        mapped = SYNONYMS.get(token, token if token in THEME_TOKENS else None)
        if mapped and mapped not in themes:
            themes.append(mapped)
    return themes


def story_cluster_key(title):
    """Return (org, number) when the headline is distinctive enough to cluster."""
    tokens = _tokens(title)
    org = _primary_org(tokens)
    numbers = _extract_numbers(" ".join(tokens))
    distinctive = [n for n in numbers if n >= 10000]
    if not org or not distinctive:
        return None
    return f"{org}|{distinctive[0]}"


def _parse_pub_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _dates_close(a, b, max_days=1):
    da, db = _parse_pub_date(a), _parse_pub_date(b)
    if not da or not db:
        return True
    return abs((da - db).days) <= max_days


def find_and_mark_story_duplicates(days=14, max_date_gap=1):
    """Merge the same event reported with slightly different headlines.

    Safe on Vercel: only scans recent relevant articles and buckets by
    company + distinctive number (e.g. Amazon + 160000).
    """
    rows = list(database.get_relevant_articles(days=days))
    if len(rows) < 2:
        return 0

    buckets = {}
    for row in rows:
        key = story_cluster_key(row["title"])
        if not key:
            continue
        buckets.setdefault(key, []).append(row)

    merged = 0
    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        bucket.sort(key=lambda row: (row["id"] or 0))
        keep = bucket[0]
        keep_themes = set(_themes(_tokens(keep["title"])))
        for other in bucket[1:]:
            if other["id"] == keep["id"]:
                continue
            if not _dates_close(keep["pub_date"], other["pub_date"], max_date_gap):
                continue
            other_themes = set(_themes(_tokens(other["title"])))
            if keep_themes and other_themes and keep_themes.isdisjoint(other_themes):
                continue
            database.mark_duplicate(other["id"], keep["id"])
            merged += 1
    return merged


def run_all_dedupes(pub_date=None):
    """URL + exact title + same-story clustering."""
    url_merged = database.dedupe_by_url()
    title_merged = database.dedupe_by_title()
    if pub_date:
        story_merged = find_and_mark_story_duplicates(days=3)
    else:
        days = 21 if os.environ.get("VERCEL") else 60
        story_merged = find_and_mark_story_duplicates(days=days)
    return {
        "url_merged": url_merged,
        "title_merged": title_merged,
        "fuzzy_merged": story_merged,
        "total_merged": url_merged + title_merged + story_merged,
    }


if __name__ == "__main__":
    import sys

    pub_date = sys.argv[1] if len(sys.argv) > 1 else None
    result = run_all_dedupes(pub_date)
    print(f"Dedup complete: {result}")
