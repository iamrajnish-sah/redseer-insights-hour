"""
main.py

End-to-end pipeline:
  1. Parse an epub newspaper file (optional, pass as arg) into articles
  2. Fetch RSS feed articles (rss_ingest.py)
  3. Fetch NewsAPI articles, if NEWSAPIKEY is set (newsapi_ingest.py)
  4. Store all new articles in SQLite
  5. Run Gemini classification/tagging/summarization on unprocessed articles
  6. Run dedup pass on today's relevant articles
  7. Print the final digest, grouped by sector

Usage:
    export GEMINI_API_KEY=...
    export NEWSAPI_KEY=...        # optional
    python3 main.py [/path/to/newspaper.epub]   # epub arg is optional
"""

import sys
import json
from sector_keywords import SECTOR_LABELS, normalize_sector_tags

import database
import epub_parser
import rss_ingest
import newsapi_ingest
import classify_and_summarize
import dedupe


def run(epub_path=None):
    database.init_db()
    all_new_articles = []
    pub_date = None

    if epub_path:
        print(f"Step 1a: Parsing epub {epub_path} ...")
        epub_articles = epub_parser.parse_epub(epub_path)
        print(f"  -> {len(epub_articles)} articles found in the epub")
        all_new_articles.extend(epub_articles)
        if epub_articles:
            pub_date = epub_articles[0].pub_date

    print("\nStep 1b: Fetching RSS feeds ...")
    rss_articles, rss_stats = rss_ingest.fetch_all()
    if rss_stats["keyword_filter"]:
        print(
            f"  -> {rss_stats['matched']} RSS items kept, "
            f"{rss_stats['skipped']} skipped by keyword filter (no Gemini needed)"
        )
    all_new_articles.extend(rss_articles)

    print("\nStep 1c: Fetching NewsAPI articles ...")
    newsapi_articles, newsapi_stats = newsapi_ingest.fetch_all()
    if newsapi_stats["matched"]:
        print(
            f"  -> {newsapi_stats['matched']} NewsAPI items kept "
            f"(pre-tagged, no Gemini needed)"
        )
    all_new_articles.extend(newsapi_articles)

    print(f"\nStep 2: Storing {len(all_new_articles)} fetched articles ...")
    new_count, _, _ = database.insert_articles(all_new_articles)
    print(f"  -> {new_count} new articles inserted (rest already in DB)")

    print("\nStep 3: Classifying + summarizing unprocessed articles via Gemini ...")
    unprocessed = database.get_unprocessed()
    print(f"  -> {len(unprocessed)} articles to classify")
    if unprocessed:
        results, stats = classify_and_summarize.classify_batch(unprocessed)
        if stats["models_used"]:
            print(f"  -> models used: {', '.join(stats['models_used'])}")
        for row in unprocessed:
            r = results.get(row["id"])
            if r:
                database.update_classification(
                    row["id"], r["is_relevant"], r["sectors"], r["summary"]
                )
        relevant = sum(1 for r in results.values() if r["is_relevant"])
        print(f"  -> {relevant} of {len(results)} marked relevant to your sectors")

    if not pub_date:
        from datetime import date
        pub_date = str(date.today())

    print("\nStep 4: Deduplicating relevant articles ...")
    dedupe.find_and_mark_duplicates(pub_date)

    print("\nStep 5: Final digest\n" + "=" * 50)
    relevant_rows = database.get_relevant_articles(pub_date=pub_date)
    by_sector = {}
    for row in relevant_rows:
        sectors = json.loads(row["sectors"] or "[]")
        for s in sectors:
            by_sector.setdefault(s, []).append(row)

    for sector_key, label in SECTOR_LABELS.items():
        rows = by_sector.get(sector_key, [])
        if not rows:
            continue
        print(f"\n--- {label} ({len(rows)}) ---")
        for row in rows:
            src_info = row["source"]
            if row["page"]:
                src_info += f", {row['page']}"
            print(f"\n* {row['title']}  [{src_info}]")
            print(f"  {row['summary']}")
            if row["url"]:
                print(f"  {row['url']}")


if __name__ == "__main__":
    epub_arg = sys.argv[1] if len(sys.argv) > 1 else None
    run(epub_arg)
