"""
dedupe.py

Finds likely-duplicate articles (same story, different URL/source).

Layers:
  1. Exact normalized title match (syndicated PR wire stories, etc.)
  2. Fuzzy title+summary similarity within title-prefix buckets
"""

import difflib
import re

import database


def normalize_title(title):
    """Stable key for headline matching across sources."""
    text = (title or "").lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _similarity(a_row, b_row):
    text_a = f"{a_row['title']} {a_row['summary'] or ''}"
    text_b = f"{b_row['title']} {b_row['summary'] or ''}"
    return difflib.SequenceMatcher(None, text_a, text_b).ratio()


def find_and_mark_duplicates(pub_date=None, high_threshold=0.82):
    """Mark near-duplicate relevant articles (optionally scoped to one pub_date)."""
    rows = list(database.get_relevant_articles(pub_date=pub_date))
    if len(rows) < 2:
        return 0

    buckets = {}
    for row in rows:
        key = normalize_title(row["title"])[:72]
        buckets.setdefault(key, []).append(row)

    merged = 0
    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                a, b = bucket[i], bucket[j]
                if a["duplicate_of"] or b["duplicate_of"]:
                    continue
                sim = _similarity(a, b)
                if sim >= high_threshold:
                    keep_id, drop_id = (a["id"], b["id"]) if a["id"] < b["id"] else (b["id"], a["id"])
                    database.mark_duplicate(drop_id, keep_id)
                    merged += 1
    return merged


def run_all_dedupes(pub_date=None):
    """URL + exact title + fuzzy passes."""
    url_merged = database.dedupe_by_url()
    title_merged = database.dedupe_by_title()
    fuzzy_merged = find_and_mark_duplicates(pub_date=pub_date)
    return {
        "url_merged": url_merged,
        "title_merged": title_merged,
        "fuzzy_merged": fuzzy_merged,
        "total_merged": url_merged + title_merged + fuzzy_merged,
    }


if __name__ == "__main__":
    import sys

    pub_date = sys.argv[1] if len(sys.argv) > 1 else None
    result = run_all_dedupes(pub_date)
    print(f"Dedup complete: {result}")
