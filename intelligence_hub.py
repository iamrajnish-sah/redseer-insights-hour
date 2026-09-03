"""
intelligence_hub.py

Independent consultant-style intelligence reports built on top of already
processed news articles. Does NOT fetch news, does NOT touch the news Gemini
pipeline (classify_and_summarize / newspaper_parser).

News processing keeps GEMINIAPIKEY.
Intelligence Hub uses a SEPARATE Gemini key:

  INTELLIGENCE_GEMINI_API_KEY  (or INTELLIGENCE_GEMINIAPIKEY)

Never falls back to the news Gemini key, so the two jobs do not share quota.

Reads already-summarized articles (not full text), caps the week to a small
unique set, and makes one Flash call per weekly brief.

Reads from SQLite and writes only to intelligence_reports.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import time
from datetime import datetime

import certifi
from google import genai
from google.genai import types

import database
from sector_keywords import SECTOR_LABELS, normalize_sector_tags

REPORT_SCHEMA_KEYS = (
    "executive_summary",
    "key_metrics",
    "strategic_moves",
    "funding",
    "partnerships",
    "leadership_changes",
    "product_launches",
    "regulations",
    "consumer_trends",
    "technology_ai",
    "competitive_landscape",
    "what_to_watch",
)

INTELLIGENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS intelligence_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sector TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    generated_by TEXT,
    report_json TEXT NOT NULL,
    report_markdown TEXT,
    status TEXT NOT NULL DEFAULT 'ready',
    source_article_ids TEXT,
    article_count INTEGER DEFAULT 0,
    model_used TEXT,
    UNIQUE(sector, start_date, end_date)
);

CREATE INDEX IF NOT EXISTS idx_intelligence_sector_dates
ON intelligence_reports(sector, start_date, end_date);
"""

SYSTEM_PROMPT = """You are a senior strategy consultant at Redseer writing a WEEKLY \
India-focused sector intelligence brief for executives.

You receive a SMALL set of already-processed article summaries (not full articles) \
for ONE sector and ONE week. Merge duplicate coverage of the same event into a single card.

Each section item is shown as its own card. The reader must understand the full story \
from the card itself and should not need to open the source article.

Every array item MUST include a "summary" field: 3 to 5 complete sentences covering \
who was involved, what happened this week, key numbers in context, why it matters for \
the India sector, and the likely business implication. Do not write one-line fragments \
or number-only blurbs such as "Rs 3,265 Cr offload". Put the number inside the narrative.

Return ONLY valid JSON with exactly these keys:
{
  "executive_summary": "5-8 sentence weekly brief covering the main stories, numbers, and so-what",
  "key_metrics":[{"company":"","metric":"","value":"","comparison":"","period":"","summary":"","source_ids":[]}],
  "strategic_moves":[{"company":"","move":"","importance":"High|Medium|Low","summary":"","source_ids":[]}],
  "funding":[{"company":"","amount":"","investor":"","summary":"","source_ids":[]}],
  "partnerships":[{"company_a":"","company_b":"","purpose":"","summary":"","source_ids":[]}],
  "leadership_changes":[{"company":"","executive":"","role":"","change":"","summary":"","source_ids":[]}],
  "product_launches":[{"company":"","product":"","description":"","summary":"","source_ids":[]}],
  "regulations":[{"authority":"","policy":"","impact":"","summary":"","source_ids":[]}],
  "consumer_trends":[{"trend":"","evidence":"","impact":"","summary":"","source_ids":[]}],
  "technology_ai":[{"company":"","technology":"","business_impact":"","summary":"","source_ids":[]}],
  "competitive_landscape":[{"company":"","development":"","implication":"","summary":"","source_ids":[]}],
  "what_to_watch":[{"watch":"short headline","summary":"","source_ids":[]}]
}

Rules:
- summary is mandatory and must be a self-contained news brief, not a headline restatement.
- Use empty arrays when a section has no evidence.
- source_ids must reference article id integers from the input.
- Do not invent companies, funding amounts, or metrics not supported by the articles.
- Focus on India and the selected sector.
- No markdown fences, no preamble.
"""

MERGE_PROMPT = """You are merging partial Redseer WEEKLY intelligence reports for the same \
sector and week into ONE final consultant brief.

Return ONLY valid JSON with the same schema as a full report. Keep each item's summary as \
a 3-5 sentence self-contained news brief. Deduplicate repeated items. Prefer \
higher-importance strategic moves and keep source_ids when present.
"""


def init_intelligence_tables(conn):
    conn.executescript(INTELLIGENCE_SCHEMA)


def _now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


INTELLIGENCE_DEFAULT_MODELS = (
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
)


def intelligence_api_key():
    """Intelligence Gemini key only — never the news GEMINIAPIKEY."""
    return (
        os.environ.get("INTELLIGENCE_GEMINI_API_KEY")
        or os.environ.get("INTELLIGENCE_GEMINIAPIKEY")
        or ""
    ).strip()


def intelligence_configured():
    return bool(intelligence_api_key())


def _model_chain():
    custom = (
        os.environ.get("INTELLIGENCE_GEMINI_MODELS")
        or os.environ.get("INTELLIGENCE_GEMINI_MODEL")
        or ""
    ).strip()
    if custom:
        return [m.strip() for m in custom.split(",") if m.strip()]
    return list(INTELLIGENCE_DEFAULT_MODELS)


def _ssl_verify():
    flag = os.environ.get("GEMINI_SSL_VERIFY", "").lower()
    if flag in ("0", "false", "no"):
        return False
    cafile = (
        os.environ.get("SSL_CERT_FILE")
        or os.environ.get("REQUESTS_CA_BUNDLE")
        or certifi.where()
    )
    return ssl.create_default_context(cafile=cafile)


def _should_try_next_model(exc):
    msg = str(exc).lower()
    if any(token in msg for token in ("401", "403", "invalid api key", "unauthorized", "api key not valid")):
        return False
    return any(
        token in msg
        for token in (
            "429", "resource_exhausted", "quota", "rate limit", "rate_limit",
            "404", "not_found", "not found", "is not supported", "is not found",
            "no longer available", "please update your code", "model_not_found",
            "does not exist", "unknown model", "503", "502", "overloaded",
            "410", "gone", "end of life",
        )
    )


def friendly_intelligence_error(exc):
    text = str(exc)
    compact = re.sub(r"\s+", " ", text).strip()
    lower = compact.lower()
    if "not set" in lower or "not configured" in lower:
        return compact[:400]
    if "api key not valid" in lower or "unauthorized" in lower or "401" in lower:
        return (
            "Intelligence Gemini key was rejected. Set INTELLIGENCE_GEMINI_API_KEY "
            "in Vercel (a second Google AI Studio key, not GEMINIAPIKEY). "
            f"({compact[:220]})"
        )
    if "429" in lower or "quota" in lower or "resource_exhausted" in lower:
        return (
            "Intelligence Gemini quota was hit. Wait a minute and retry, or use a "
            "fresh INTELLIGENCE_GEMINI_API_KEY. News processing uses a different key. "
            f"({compact[:220]})"
        )
    if "404" in lower or "not_found" in lower or "not found" in lower:
        return (
            "Gemini rejected the intelligence model ID. Retry Generate Intelligence; "
            "Flash fallbacks are tried automatically. "
            f"({compact[:220]})"
        )
    return compact[:400]


def friendly_gemini_error(exc):
    """Backward-compatible alias used by app.py error handling."""
    return friendly_intelligence_error(exc)


def _article_cap():
    return max(8, int(os.environ.get("INTELLIGENCE_ARTICLE_CAP", "24")))


def _batch_size():
    return max(8, int(os.environ.get("INTELLIGENCE_ARTICLE_BATCH", "24")))


def _summary_chars():
    return max(160, int(os.environ.get("INTELLIGENCE_SUMMARY_CHARS", "360")))


def _get_client():
    api_key = intelligence_api_key()
    if not api_key:
        raise RuntimeError(
            "INTELLIGENCE_GEMINI_API_KEY is not set. Add a second Google AI Studio "
            "key in Vercel for Intelligence Hub. GEMINIAPIKEY is only for news/newspaper."
        )
    http_options = types.HttpOptions(
        client_args={"verify": _ssl_verify()},
        async_client_args={"verify": _ssl_verify()},
    )
    return genai.Client(api_key=api_key, http_options=http_options)


def _empty_report():
    return {
        "executive_summary": "",
        "key_metrics": [],
        "strategic_moves": [],
        "funding": [],
        "partnerships": [],
        "leadership_changes": [],
        "product_launches": [],
        "regulations": [],
        "consumer_trends": [],
        "technology_ai": [],
        "competitive_landscape": [],
        "what_to_watch": [],
    }


def _normalize_item(item):
    if not isinstance(item, dict):
        return None
    cleaned = dict(item)
    cleaned["summary"] = str(cleaned.get("summary") or "").strip()
    raw_ids = cleaned.get("source_ids") or []
    ids = []
    for value in raw_ids:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    cleaned["source_ids"] = ids
    return cleaned


def _normalize_watch_item(item):
    if isinstance(item, str) and item.strip():
        text = item.strip()
        return {"watch": text, "summary": text, "source_ids": []}
    cleaned = _normalize_item(item)
    if not cleaned:
        return None
    watch = str(
        cleaned.get("watch")
        or cleaned.get("item")
        or cleaned.get("headline")
        or ""
    ).strip()
    summary = cleaned.get("summary") or watch
    if not watch and not summary:
        return None
    cleaned["watch"] = watch or summary.split(".")[0][:80]
    cleaned["summary"] = summary
    return cleaned


def _normalize_report(payload):
    report = _empty_report()
    if not isinstance(payload, dict):
        return report
    report["executive_summary"] = str(payload.get("executive_summary") or "").strip()
    for key in REPORT_SCHEMA_KEYS:
        if key == "executive_summary":
            continue
        value = payload.get(key)
        if key == "what_to_watch":
            cleaned = []
            for item in (value or []):
                watch = _normalize_watch_item(item)
                if watch:
                    cleaned.append(watch)
            report[key] = cleaned
        elif isinstance(value, list):
            cleaned = []
            for item in value:
                normalized = _normalize_item(item)
                if normalized:
                    cleaned.append(normalized)
            report[key] = cleaned
        else:
            report[key] = []
    return report


def _parse_json_response(text):
    text = (text or "").strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"</?think>", "", text, flags=re.IGNORECASE)
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return _normalize_report(json.loads(text))
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return _normalize_report(json.loads(text[start:end + 1]))
        raise


def _call_intelligence_model(client, system_prompt, user_payload):
    last_error = None
    contents = [system_prompt, json.dumps(user_payload, ensure_ascii=False)]
    for model in _model_chain():
        try:
            resp = client.models.generate_content(
                model=model,
                contents=contents,
                config={"response_mime_type": "application/json"},
            )
            return _parse_json_response(resp.text), model
        except Exception as exc:
            last_error = exc
            if not _should_try_next_model(exc):
                raise
            print(f"  [intelligence] {model} failed — trying next Gemini model ...")
            time.sleep(0.8)
    raise RuntimeError(
        friendly_intelligence_error(
            last_error or RuntimeError("All Intelligence Gemini models failed")
        )
    )


def validate_sector(sector):
    sector = (sector or "").strip()
    if sector not in SECTOR_LABELS:
        raise ValueError(f"Unknown sector: {sector}")
    return sector


def validate_dates(start_date, end_date=None):
    start_date = (start_date or "").strip()
    end_date = (end_date or start_date or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", start_date):
        raise ValueError("start_date must be YYYY-MM-DD")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", end_date):
        raise ValueError("end_date must be YYYY-MM-DD")
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    return start_date, end_date


def period_label(start_date, end_date=None):
    start_date, end_date = validate_dates(start_date, end_date)
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")

    def fmt(day):
        return f"{day.day} {day.strftime('%b %Y')}"

    span = (end - start).days
    if span == 6 and start.weekday() == 0:
        iso = start.isocalendar()
        return f"Week {iso[1]}, {iso[0]} · {fmt(start)} – {fmt(end)}"
    if start_date == end_date:
        return fmt(start)
    return f"{fmt(start)} – {fmt(end)}"


def fetch_sector_articles(sector, start_date, end_date):
    """Read-only query of already processed, relevant articles."""
    sector = validate_sector(sector)
    start_date, end_date = validate_dates(start_date, end_date)
    query = """
        SELECT id, title, subtitle, summary, source, pub_date, url, resolved_url,
               sectors, origin, byline
        FROM articles
        WHERE relevant = 1
          AND duplicate_of IS NULL
          AND processed = 1
          AND pub_date >= ?
          AND pub_date <= ?
        ORDER BY pub_date DESC, id DESC
    """
    with database.get_conn() as conn:
        rows = conn.execute(query, [start_date, end_date]).fetchall()

    articles = []
    for row in rows:
        item = dict(row)
        item["sectors"] = normalize_sector_tags(json.loads(item.get("sectors") or "[]"))
        if sector not in item["sectors"]:
            continue
        articles.append(item)
    return articles


def _normalize_event_key(title):
    text = (title or "").lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:90]


def consolidate_articles(articles):
    """Remove near-duplicate stories and merge same-event coverage."""
    seen_urls = set()
    groups = {}
    for article in articles:
        url = (article.get("resolved_url") or article.get("url") or "").strip().rstrip("/")
        if url:
            if url in seen_urls:
                continue
            seen_urls.add(url)

        key = _normalize_event_key(article.get("title")) or f"id:{article['id']}"
        bucket = groups.setdefault(key, [])
        if not bucket:
            article = dict(article)
            article["related_ids"] = []
            bucket.append(article)
            continue

        # Merge into the first (newest) story by appending alternate sources.
        primary = bucket[0]
        alts = primary.setdefault("related_ids", [])
        if article["id"] not in alts and article["id"] != primary["id"]:
            alts.append(article["id"])
            extra = (article.get("summary") or "").strip()
            if extra and extra not in (primary.get("summary") or ""):
                primary["summary"] = (
                    (primary.get("summary") or "").rstrip() + " | Related: " + extra
                ).strip()

    consolidated = [bucket[0] for bucket in groups.values()]
    consolidated.sort(key=lambda a: (a.get("pub_date") or "", a.get("id") or 0), reverse=True)
    return consolidated


def _truncate(text, limit):
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:")
    return (cut or text[:limit]) + "…"


def _article_payload(article):
    return {
        "id": article["id"],
        "title": article.get("title") or "",
        "date": article.get("pub_date") or "",
        "source": article.get("source") or "",
        "summary": _truncate(
            article.get("summary") or article.get("subtitle") or "",
            _summary_chars(),
        ),
    }


def prepare_intelligence_articles(articles):
    """Dedupe, then keep only the newest unique stories so one Flash call stays small."""
    consolidated = consolidate_articles(articles)
    cap = _article_cap()
    selected = consolidated[:cap]
    return selected, {
        "source_count": len(articles),
        "consolidated_count": len(consolidated),
        "used_count": len(selected),
        "capped": len(consolidated) > cap,
        "article_cap": cap,
    }


def _merge_partial_reports(partials):
    if not partials:
        return _empty_report()
    if len(partials) == 1:
        return partials[0]

    client = _get_client()
    merged, _ = _call_intelligence_model(
        client,
        MERGE_PROMPT,
        {
            "partial_reports": partials,
            "instruction": "Produce one deduplicated final report.",
        },
    )
    return merged


def generate_report_json(sector, start_date, end_date, articles):
    label = SECTOR_LABELS.get(sector, sector)
    selected, _stats = prepare_intelligence_articles(articles)
    payloads = [_article_payload(a) for a in selected]
    client = _get_client()
    batch = _batch_size()
    partials = []
    models = []

    for index in range(0, len(payloads), batch):
        chunk = payloads[index:index + batch]
        user_payload = {
            "sector": sector,
            "sector_label": label,
            "cadence": "weekly",
            "start_date": start_date,
            "end_date": end_date,
            "period_label": period_label(start_date, end_date),
            "article_count": len(chunk),
            "articles": chunk,
            "instruction": (
                "These are already-processed short summaries, not full articles. "
                "Synthesize a weekly brief. One card per distinct story."
            ),
        }
        report, model = _call_intelligence_model(client, SYSTEM_PROMPT, user_payload)
        partials.append(report)
        models.append(model)
        if index + batch < len(payloads):
            time.sleep(0.8)

    final = _merge_partial_reports(partials) if len(partials) > 1 else partials[0]
    return final, selected, models[-1] if models else None


def report_to_markdown(sector, start_date, end_date, report, articles=None):
    label = SECTOR_LABELS.get(sector, sector)
    lines = [
        f"# Redseer Intelligence Hub — {label}",
        f"**Period:** {period_label(start_date, end_date)}",
        "",
        "## Executive Summary",
        report.get("executive_summary") or "_No summary available._",
        "",
    ]

    def headline(row, fields):
        parts = [str(row.get(field) or "").strip() for field in fields]
        return " — ".join(part for part in parts if part) or "Update"

    def section(title, rows, fields):
        lines.append(f"## {title}")
        if not rows:
            lines.append("_None identified._")
            lines.append("")
            return
        for row in rows:
            if isinstance(row, str):
                lines.append(f"- {row}")
                continue
            lines.append(f"### {headline(row, fields)}")
            summary = (row.get("summary") or "").strip()
            if summary:
                lines.append(summary)
            else:
                extras = []
                for field in fields[1:]:
                    val = row.get(field)
                    if val:
                        extras.append(f"{field.replace('_', ' ')}: {val}")
                if extras:
                    lines.append(" | ".join(extras))
            source_ids = row.get("source_ids") or []
            if source_ids:
                lines.append(f"Source ids: {', '.join(str(i) for i in source_ids)}")
            lines.append("")
        lines.append("")

    section("Key Metrics", report.get("key_metrics"), ["company", "metric", "value"])
    section("Strategic Moves", report.get("strategic_moves"), ["company", "move", "importance"])
    section("Funding", report.get("funding"), ["company", "amount", "investor"])
    section("Partnerships", report.get("partnerships"), ["company_a", "company_b", "purpose"])
    section("Leadership Changes", report.get("leadership_changes"), ["company", "executive", "role", "change"])
    section("Product Launches", report.get("product_launches"), ["company", "product"])
    section("Regulations", report.get("regulations"), ["authority", "policy"])
    section("Consumer Trends", report.get("consumer_trends"), ["trend"])
    section("Technology & AI", report.get("technology_ai"), ["company", "technology"])
    section("Competitive Landscape", report.get("competitive_landscape"), ["company", "development"])
    section("What to Watch", report.get("what_to_watch"), ["watch"])

    if articles:
        lines.append("## Source Articles")
        for article in articles:
            link = article.get("resolved_url") or article.get("url") or ""
            title = article.get("title") or f"Article {article['id']}"
            if link:
                lines.append(f"- [{title}]({link}) ({article.get('pub_date')})")
            else:
                lines.append(f"- {title} ({article.get('pub_date')})")
        lines.append("")

    return "\n".join(lines)


def _row_to_report(row, include_json=True):
    if not row:
        return None
    data = dict(row)
    data["source_article_ids"] = json.loads(data.get("source_article_ids") or "[]")
    data["period_label"] = period_label(data["start_date"], data["end_date"])
    if include_json:
        data["report"] = _normalize_report(json.loads(data.get("report_json") or "{}"))
    else:
        data.pop("report_json", None)
    return data


def get_cached_report(sector, start_date, end_date):
    sector = validate_sector(sector)
    start_date, end_date = validate_dates(start_date, end_date)
    with database.get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM intelligence_reports
               WHERE sector = ? AND start_date = ? AND end_date = ?
               ORDER BY id DESC LIMIT 1""",
            (sector, start_date, end_date),
        ).fetchone()
    return _row_to_report(row)


def reports_for_period(sector, start_date, end_date=None):
    """Public lookup: exact match first, then any brief overlapping the dates."""
    sector = validate_sector(sector)
    start_date, end_date = validate_dates(start_date, end_date)
    exact = get_cached_report(sector, start_date, end_date)
    with database.get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM intelligence_reports
               WHERE sector = ? AND start_date <= ? AND end_date >= ?
               ORDER BY generated_at DESC, id DESC""",
            (sector, end_date, start_date),
        ).fetchall()
    reports = [_row_to_report(row) for row in rows]
    if exact:
        reports = [exact] + [r for r in reports if r.get("id") != exact.get("id")]
    return reports


def get_report(report_id):
    with database.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM intelligence_reports WHERE id = ?",
            (report_id,),
        ).fetchone()
    return _row_to_report(row)


def list_reports(sector=None, limit=50):
    query = "SELECT id, sector, start_date, end_date, generated_at, generated_by, status, article_count, model_used FROM intelligence_reports"
    params = []
    if sector:
        query += " WHERE sector = ?"
        params.append(validate_sector(sector))
    query += " ORDER BY generated_at DESC, id DESC LIMIT ?"
    params.append(int(limit))
    with database.get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    reports = []
    for row in rows:
        item = dict(row)
        item["period_label"] = period_label(item["start_date"], item["end_date"])
        reports.append(item)
    return reports


def delete_report(report_id):
    with database.get_conn() as conn:
        cur = conn.execute("DELETE FROM intelligence_reports WHERE id = ?", (report_id,))
        if cur.rowcount == 0:
            raise ValueError("Report not found")
    database.persist()


def _save_report(sector, start_date, end_date, report, articles, generated_by, model_used, replace_id=None):
    markdown = report_to_markdown(sector, start_date, end_date, report, articles)
    source_ids = [a["id"] for a in articles]
    related = []
    for article in articles:
        related.extend(article.get("related_ids") or [])
    all_ids = sorted(set(source_ids + related))
    now = _now_iso()
    with database.get_conn() as conn:
        if replace_id:
            conn.execute(
                """UPDATE intelligence_reports
                   SET generated_at = ?, generated_by = ?, report_json = ?,
                       report_markdown = ?, status = ?, source_article_ids = ?,
                       article_count = ?, model_used = ?
                   WHERE id = ?""",
                (
                    now, generated_by, json.dumps(report, ensure_ascii=False),
                    markdown, "ready", json.dumps(all_ids), len(articles),
                    model_used, replace_id,
                ),
            )
            report_id = replace_id
        else:
            existing = conn.execute(
                """SELECT id FROM intelligence_reports
                   WHERE sector = ? AND start_date = ? AND end_date = ?""",
                (sector, start_date, end_date),
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE intelligence_reports
                       SET generated_at = ?, generated_by = ?, report_json = ?,
                           report_markdown = ?, status = ?, source_article_ids = ?,
                           article_count = ?, model_used = ?
                       WHERE id = ?""",
                    (
                        now, generated_by, json.dumps(report, ensure_ascii=False),
                        markdown, "ready", json.dumps(all_ids), len(articles),
                        model_used, existing["id"],
                    ),
                )
                report_id = existing["id"]
            else:
                cur = conn.execute(
                    """INSERT INTO intelligence_reports
                       (sector, start_date, end_date, generated_at, generated_by,
                        report_json, report_markdown, status, source_article_ids,
                        article_count, model_used)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?)""",
                    (
                        sector, start_date, end_date, now, generated_by,
                        json.dumps(report, ensure_ascii=False), markdown,
                        json.dumps(all_ids), len(articles), model_used,
                    ),
                )
                report_id = cur.lastrowid
    database.persist()
    return get_report(report_id)


def generate_or_get_report(sector, start_date, end_date=None, force=False, generated_by="admin"):
    sector = validate_sector(sector)
    start_date, end_date = validate_dates(start_date, end_date)

    if not force:
        cached = get_cached_report(sector, start_date, end_date)
        if cached and cached.get("status") == "ready":
            cached["cached"] = True
            return cached

    if not intelligence_configured():
        raise RuntimeError(
            "INTELLIGENCE_GEMINI_API_KEY is not configured. "
            "Intelligence Hub uses a second Gemini key; news processing still uses GEMINIAPIKEY."
        )

    articles = fetch_sector_articles(sector, start_date, end_date)
    if not articles:
        raise LookupError("No processed articles available for the selected period.")

    report, consolidated, model_used = generate_report_json(
        sector, start_date, end_date, articles
    )
    saved = _save_report(
        sector, start_date, end_date, report, consolidated, generated_by, model_used
    )
    saved["cached"] = False
    return saved


def enrich_report_with_sources(report_row):
    """Attach source article metadata for UI linking."""
    if not report_row:
        return None
    ids = report_row.get("source_article_ids") or []
    sources = []
    if ids:
        placeholders = ",".join("?" * len(ids))
        with database.get_conn() as conn:
            rows = conn.execute(
                f"""SELECT id, title, pub_date, source, url, resolved_url, summary
                    FROM articles WHERE id IN ({placeholders})
                    ORDER BY pub_date DESC, id DESC""",
                ids,
            ).fetchall()
        sources = [dict(row) for row in rows]
    report_row["sources"] = sources
    report_row["sector_label"] = SECTOR_LABELS.get(report_row["sector"], report_row["sector"])
    report_row["period_label"] = report_row.get("period_label") or period_label(
        report_row["start_date"], report_row["end_date"]
    )
    return report_row
