"""
database.py

Simple SQLite storage for articles. One file, no server needed.
Good enough for an internal single-office tool; can swap for Postgres later
if this grows into a multi-user product.
"""

import os
import sqlite3
import json
import re
from datetime import datetime
from contextlib import contextmanager

DB_PATH = os.environ.get(
    "DATABASE_PATH",
    "/tmp/news.db" if os.environ.get("VERCEL") else "news.db",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origin TEXT,
    source TEXT,
    pub_date TEXT,
    page TEXT,
    article_id TEXT,
    url TEXT,
    title TEXT,
    subtitle TEXT,
    byline TEXT,
    body TEXT,
    relevant INTEGER,
    sectors TEXT,
    summary TEXT,
    duplicate_of INTEGER,
    processed INTEGER DEFAULT 0,
    image_url TEXT,
    title_key TEXT,
    UNIQUE(origin, source, pub_date, page, article_id, url)
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute(SCHEMA)
        try:
            conn.execute("ALTER TABLE articles ADD COLUMN fetched_at TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE articles ADD COLUMN image_url TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE articles ADD COLUMN title_key TEXT")
        except sqlite3.OperationalError:
            pass
        _backfill_title_keys(conn)
        dedupe_by_url(conn)
        dedupe_by_title(conn)
        try:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_articles_title_key "
                "ON articles(title_key) "
                "WHERE title_key IS NOT NULL AND title_key != '' AND duplicate_of IS NULL"
            )
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_url "
                "ON articles(url) "
                "WHERE url IS NOT NULL AND url != '' AND duplicate_of IS NULL"
            )
        except sqlite3.OperationalError:
            pass


def _now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _normalize_url(url):
    if not url:
        return None
    url = url.strip()
    return url.rstrip("/") or url


def _normalize_title_key(title):
    text = (title or "").lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _backfill_title_keys(conn):
    rows = conn.execute(
        "SELECT id, title FROM articles WHERE title_key IS NULL OR title_key = ''"
    ).fetchall()
    for row in rows:
        key = _normalize_title_key(row["title"])
        if key:
            conn.execute(
                "UPDATE articles SET title_key = ? WHERE id = ?",
                (key, row["id"]),
            )


def _refresh_existing(conn, existing_id, fetched_at, image_url=None):
    if image_url:
        conn.execute(
            """UPDATE articles
               SET fetched_at = ?, image_url = COALESCE(image_url, ?)
               WHERE id = ?""",
            (fetched_at, image_url, existing_id),
        )
    else:
        conn.execute(
            "UPDATE articles SET fetched_at = ? WHERE id = ?",
            (fetched_at, existing_id),
        )


def dedupe_by_url(conn=None):
    """Mark duplicate rows that share the same URL (keeps the oldest row)."""
    def _run(c):
        groups = c.execute(
            """SELECT url, MIN(id) AS keep_id
               FROM articles
               WHERE url IS NOT NULL AND url != '' AND duplicate_of IS NULL
               GROUP BY url
               HAVING COUNT(*) > 1"""
        ).fetchall()
        merged = 0
        for row in groups:
            keep_id = row["keep_id"]
            dupes = c.execute(
                """SELECT id FROM articles
                   WHERE url = ? AND id != ? AND duplicate_of IS NULL""",
                (row["url"], keep_id),
            ).fetchall()
            for dupe in dupes:
                c.execute(
                    "UPDATE articles SET duplicate_of = ? WHERE id = ?",
                    (keep_id, dupe["id"]),
                )
                merged += 1
        return merged

    if conn is not None:
        return _run(conn)
    with get_conn() as c:
        return _run(c)


def dedupe_by_title(conn=None):
    """Mark duplicate rows that share the same normalized headline (keeps oldest)."""
    def _run(c):
        groups = c.execute(
            """SELECT title_key, MIN(id) AS keep_id
               FROM articles
               WHERE title_key IS NOT NULL AND title_key != ''
               AND duplicate_of IS NULL
               GROUP BY title_key
               HAVING COUNT(*) > 1"""
        ).fetchall()
        merged = 0
        for row in groups:
            keep_id = row["keep_id"]
            dupes = c.execute(
                """SELECT id FROM articles
                   WHERE title_key = ? AND id != ? AND duplicate_of IS NULL""",
                (row["title_key"], keep_id),
            ).fetchall()
            for dupe in dupes:
                c.execute(
                    "UPDATE articles SET duplicate_of = ? WHERE id = ?",
                    (keep_id, dupe["id"]),
                )
                merged += 1
        return merged

    if conn is not None:
        return _run(conn)
    with get_conn() as c:
        return _run(c)


def insert_articles(articles):
    """Insert parsed Article objects, skipping duplicates already stored.

    If an article has pre_classified=True (keyword-filtered RSS), it is stored
    as already relevant/processed so Gemini is not needed.

    Returns (inserted_count, new_ids, refreshed_ids)."""
    inserted = 0
    inserted_ids = []
    refreshed_ids = []
    fetched_at = _now_iso()
    with get_conn() as conn:
        for a in articles:
            origin = getattr(a, "origin", "epub")
            url = _normalize_url(getattr(a, "url", None))
            page = getattr(a, "page", None)
            article_id = getattr(a, "article_id", None)
            image_url = getattr(a, "image_url", None) or None
            title_key = _normalize_title_key(a.title)
            pre_classified = getattr(a, "pre_classified", False)

            if url:
                existing = conn.execute(
                    """SELECT id FROM articles
                       WHERE url = ? AND duplicate_of IS NULL
                       ORDER BY id LIMIT 1""",
                    (url,),
                ).fetchone()
                if existing:
                    _refresh_existing(conn, existing["id"], fetched_at, image_url)
                    refreshed_ids.append(existing["id"])
                    continue

            if title_key:
                existing = conn.execute(
                    """SELECT id FROM articles
                       WHERE title_key = ? AND duplicate_of IS NULL
                       ORDER BY id LIMIT 1""",
                    (title_key,),
                ).fetchone()
                if existing:
                    _refresh_existing(conn, existing["id"], fetched_at, image_url)
                    refreshed_ids.append(existing["id"])
                    continue

            try:
                if pre_classified:
                    sectors = getattr(a, "sectors", []) or []
                    summary = getattr(a, "auto_summary", "") or getattr(a, "summary", "") or a.title
                    cur = conn.execute(
                        """INSERT INTO articles
                           (origin, source, pub_date, page, article_id, url, title, subtitle, byline, body,
                            relevant, sectors, summary, processed, fetched_at, image_url, title_key)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 1, ?, ?, ?)""",
                        (origin, a.source, a.pub_date, page, article_id, url,
                         a.title, a.subtitle, a.byline, a.body,
                         json.dumps(sectors), summary, fetched_at, image_url, title_key),
                    )
                else:
                    cur = conn.execute(
                        """INSERT INTO articles
                           (origin, source, pub_date, page, article_id, url, title, subtitle, byline, body, fetched_at, image_url, title_key)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (origin, a.source, a.pub_date, page, article_id, url,
                         a.title, a.subtitle, a.byline, a.body, fetched_at, image_url, title_key),
                    )
                inserted += 1
                inserted_ids.append(cur.lastrowid)
            except sqlite3.IntegrityError:
                if url:
                    existing = conn.execute(
                        """SELECT id FROM articles
                           WHERE url = ? AND duplicate_of IS NULL
                           ORDER BY id LIMIT 1""",
                        (url,),
                    ).fetchone()
                    if existing:
                        _refresh_existing(conn, existing["id"], fetched_at, image_url)
                        refreshed_ids.append(existing["id"])
                        continue
                if title_key:
                    existing = conn.execute(
                        """SELECT id FROM articles
                           WHERE title_key = ? AND duplicate_of IS NULL
                           ORDER BY id LIMIT 1""",
                        (title_key,),
                    ).fetchone()
                    if existing:
                        _refresh_existing(conn, existing["id"], fetched_at, image_url)
                        refreshed_ids.append(existing["id"])
    return inserted, inserted_ids, refreshed_ids


def get_unprocessed(limit=None, max_days=None, origins=None):
    query = "SELECT * FROM articles WHERE processed = 0"
    params = []
    if max_days is not None:
        query += " AND pub_date >= date('now', ?)"
        params.append(f"-{int(max_days)} days")
    if origins:
        placeholders = ",".join("?" * len(origins))
        query += f" AND origin IN ({placeholders})"
        params.extend(origins)
    query += " ORDER BY pub_date DESC, id DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(int(limit))
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return rows


def get_unprocessed_stats(max_days=None, origins=None):
    """Counts pending articles, grouped by origin."""
    query = "SELECT origin, COUNT(*) c FROM articles WHERE processed = 0"
    params = []
    if max_days is not None:
        query += " AND pub_date >= date('now', ?)"
        params.append(f"-{int(max_days)} days")
    if origins:
        placeholders = ",".join("?" * len(origins))
        query += f" AND origin IN ({placeholders})"
        params.extend(origins)
    query += " GROUP BY origin ORDER BY c DESC"
    with get_conn() as conn:
        return {r["origin"]: r["c"] for r in conn.execute(query, params).fetchall()}


def update_classification(article_id, relevant, sectors, summary):
    with get_conn() as conn:
        conn.execute(
            """UPDATE articles SET relevant=?, sectors=?, summary=?, processed=1
               WHERE id=?""",
            (int(relevant), json.dumps(sectors), summary, article_id),
        )


def mark_duplicate(article_id, duplicate_of_id):
    with get_conn() as conn:
        conn.execute(
            "UPDATE articles SET duplicate_of=? WHERE id=?",
            (duplicate_of_id, article_id),
        )


def get_pub_dates():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT pub_date FROM articles ORDER BY pub_date DESC"
        ).fetchall()
    return [r["pub_date"] for r in rows]


def get_stats():
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"]
        unprocessed = conn.execute("SELECT COUNT(*) c FROM articles WHERE processed=0").fetchone()["c"]
        relevant = conn.execute(
            "SELECT COUNT(*) c FROM articles WHERE relevant=1 AND duplicate_of IS NULL"
        ).fetchone()["c"]
    return {"total": total, "unprocessed": unprocessed, "relevant": relevant}


def get_relevant_articles(pub_date=None, sector=None, search=None):
    query = "SELECT * FROM articles WHERE relevant=1 AND duplicate_of IS NULL"
    params = []
    if pub_date:
        query += " AND pub_date=?"
        params.append(pub_date)
    if sector:
        if sector == "cross_sector":
            query += " AND (sectors LIKE ? OR sectors LIKE ?)"
            params.extend(["%cross_sector%", "%other_relevant%"])
        else:
            query += " AND sectors LIKE ?"
            params.append(f"%{sector}%")
    if search:
        terms = [t.strip() for t in search.split() if t.strip()]
        if terms:
            term_clauses = []
            for term in terms:
                pattern = f"%{term}%"
                term_clauses.append(
                    "(title LIKE ? OR summary LIKE ? OR body LIKE ? OR source LIKE ?)"
                )
                params.extend([pattern, pattern, pattern, pattern])
            query += " AND (" + " OR ".join(term_clauses) + ")"
    query += " ORDER BY COALESCE(fetched_at, pub_date) DESC, id DESC"
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return rows


def reconcile_ride_hailing_tags():
    """Re-apply ride-hailing rules to stored articles (fixes old loose tagging)."""
    from sector_keywords import is_ride_hailing_relevant, normalize_sector_tags

    added = 0
    removed = 0
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, title, subtitle, body, summary, sectors
               FROM articles
               WHERE duplicate_of IS NULL AND relevant = 1"""
        ).fetchall()
        for row in rows:
            sectors = normalize_sector_tags(json.loads(row["sectors"] or "[]"))
            should_tag = is_ride_hailing_relevant(
                row["title"],
                row["body"] or row["summary"] or "",
                row["subtitle"] or "",
            )
            has_tag = "ride_hailing" in sectors

            if should_tag and not has_tag:
                sectors.append("ride_hailing")
                conn.execute(
                    "UPDATE articles SET sectors = ? WHERE id = ?",
                    (json.dumps(sectors), row["id"]),
                )
                added += 1
            elif not should_tag and has_tag:
                sectors = [s for s in sectors if s != "ride_hailing"]
                if sectors:
                    conn.execute(
                        "UPDATE articles SET sectors = ? WHERE id = ?",
                        (json.dumps(sectors), row["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE articles SET sectors = '[]', relevant = 0 WHERE id = ?",
                        (row["id"],),
                    )
                removed += 1

    return {"added": added, "removed": removed}
