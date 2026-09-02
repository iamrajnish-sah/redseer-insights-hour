"""
intelligence_hub.py

Independent consultant-style intelligence reports built on top of already
processed news articles. Does NOT fetch news, does NOT touch the news Gemini
pipeline (classify_and_summarize / newspaper_parser).

News processing keeps GEMINIAPIKEY.
Intelligence Hub uses a separate NVIDIA NIM key:

  NVIDIA_API_KEY  (aliases: NVIDIAAPIKEY, INTELLIGENCE_NVIDIA_API_KEY)

Never falls back to the Gemini news key.

Reads articles from the existing SQLite store and writes only to
intelligence_reports.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime

import certifi
import requests

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

SYSTEM_PROMPT = """You are a senior strategy consultant at Redseer writing an India-focused \
sector intelligence brief for executives.

You receive a consolidated list of already-processed news articles for ONE sector and \
ONE date range. Merge duplicate coverage of the same event. Prefer hard facts, numbers, \
company names, and business implications over fluff.

Return ONLY valid JSON with exactly these keys:
{
  "executive_summary": "3-6 sentence consultant brief",
  "key_metrics":[{"company":"","metric":"","value":"","comparison":"","period":"","source_ids":[]}],
  "strategic_moves":[{"company":"","move":"","importance":"High|Medium|Low","source_ids":[]}],
  "funding":[{"company":"","amount":"","investor":"","source_ids":[]}],
  "partnerships":[{"company_a":"","company_b":"","purpose":"","source_ids":[]}],
  "leadership_changes":[{"company":"","executive":"","role":"","change":"","source_ids":[]}],
  "product_launches":[{"company":"","product":"","description":"","source_ids":[]}],
  "regulations":[{"authority":"","policy":"","impact":"","source_ids":[]}],
  "consumer_trends":[{"trend":"","evidence":"","impact":"","source_ids":[]}],
  "technology_ai":[{"company":"","technology":"","business_impact":"","source_ids":[]}],
  "competitive_landscape":[{"company":"","development":"","implication":"","source_ids":[]}],
  "what_to_watch":["...", "...", "..."]
}

Rules:
- Use empty arrays when a section has no evidence.
- source_ids must reference article id integers from the input.
- Do not invent companies, funding amounts, or metrics not supported by the articles.
- Focus on India and the selected sector.
- No markdown fences, no preamble.
"""

MERGE_PROMPT = """You are merging partial Redseer intelligence reports for the same sector \
and date range into ONE final consultant brief.

Return ONLY valid JSON with the same schema as a full report. Deduplicate repeated items. \
Prefer higher-importance strategic moves and keep source_ids when present.
"""


def init_intelligence_tables(conn):
    conn.executescript(INTELLIGENCE_SCHEMA)


def _now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


NVIDIA_DEFAULT_BASE = "https://integrate.api.nvidia.com/v1"
NVIDIA_DEFAULT_MODELS = (
    "nvidia/llama-3.3-nemotron-super-49b-v1.5",
    "nvidia/llama-3.3-nemotron-super-49b-v1",
    "meta/llama-3.3-70b-instruct",
    "meta/llama-3.1-70b-instruct",
)


def intelligence_api_key():
    """NVIDIA key only — never the news Gemini key."""
    return (
        os.environ.get("NVIDIA_API_KEY")
        or os.environ.get("NVIDIAAPIKEY")
        or os.environ.get("INTELLIGENCE_NVIDIA_API_KEY")
        or ""
    ).strip()


def intelligence_configured():
    return bool(intelligence_api_key())


def _model_chain():
    custom = (
        os.environ.get("NVIDIA_MODELS")
        or os.environ.get("NVIDIA_MODEL")
        or os.environ.get("INTELLIGENCE_NVIDIA_MODELS")
        or ""
    ).strip()
    if custom:
        return [m.strip() for m in custom.split(",") if m.strip()]
    return list(NVIDIA_DEFAULT_MODELS)


def _nvidia_base_url():
    return (
        os.environ.get("NVIDIA_API_BASE")
        or os.environ.get("NVIDIA_BASE_URL")
        or NVIDIA_DEFAULT_BASE
    ).rstrip("/")


def _ssl_verify():
    flag = (
        os.environ.get("NVIDIA_SSL_VERIFY")
        or os.environ.get("GEMINI_SSL_VERIFY")
        or ""
    ).lower()
    if flag in ("0", "false", "no"):
        return False
    return (
        os.environ.get("SSL_CERT_FILE")
        or os.environ.get("REQUESTS_CA_BUNDLE")
        or certifi.where()
    )


def _should_try_next_model(exc):
    msg = str(exc).lower()
    if any(token in msg for token in ("401", "403", "invalid api key", "unauthorized")):
        return False
    return any(
        token in msg
        for token in (
            "429", "resource_exhausted", "quota", "rate limit", "rate_limit",
            "404", "not_found", "not found", "is not supported", "is not found",
            "no longer available", "please update your code", "model_not_found",
            "does not exist", "unknown model", "503", "502", "overloaded",
        )
    )


def friendly_intelligence_error(exc):
    text = str(exc)
    compact = re.sub(r"\s+", " ", text).strip()
    lower = compact.lower()
    if "401" in lower or "unauthorized" in lower or "invalid api key" in lower:
        return (
            "NVIDIA rejected the API key. Set NVIDIA_API_KEY in Vercel environment "
            "variables (build.nvidia.com). "
            f"({compact[:220]})"
        )
    if "not set" in lower or "not configured" in lower:
        return compact[:400]
    if "404" in lower or "not_found" in lower or "unknown model" in lower:
        return (
            "NVIDIA rejected the model ID. Set NVIDIA_MODELS to a comma-separated "
            "list from build.nvidia.com, then retry Generate Intelligence. "
            f"({compact[:220]})"
        )
    return compact[:400]


def friendly_gemini_error(exc):
    """Backward-compatible alias used by app.py error handling."""
    return friendly_intelligence_error(exc)


def _batch_size():
    return int(os.environ.get("INTELLIGENCE_ARTICLE_BATCH", "25"))


def _get_client():
    api_key = intelligence_api_key()
    if not api_key:
        raise RuntimeError(
            "NVIDIA_API_KEY is not set. Add it in Vercel environment variables "
            "to generate Intelligence Hub briefs (news pipeline Gemini key is not used)."
        )
    return {
        "api_key": api_key,
        "base_url": _nvidia_base_url(),
        "verify": _ssl_verify(),
        "timeout": int(os.environ.get("NVIDIA_TIMEOUT", "120")),
        "max_tokens": int(os.environ.get("NVIDIA_MAX_TOKENS", "8192")),
    }


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
            report[key] = [str(item).strip() for item in (value or []) if str(item).strip()]
        elif isinstance(value, list):
            cleaned = []
            for item in value:
                if isinstance(item, dict):
                    cleaned.append(item)
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


def _extract_message_text(payload):
    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError("NVIDIA API returned no choices")
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
            else:
                parts.append(str(part))
        content = "".join(parts)
    if not (content or "").strip():
        content = message.get("reasoning_content") or ""
    if not (content or "").strip():
        raise RuntimeError("NVIDIA API returned an empty message")
    return content


def _is_nemotron(model):
    return "nemotron" in (model or "").lower()


def _nvidia_chat(client, model, system_prompt, user_payload):
    system_content = system_prompt
    if _is_nemotron(model):
        system_content = "detailed thinking off\n\n" + system_prompt
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "temperature": 0.2 if not _is_nemotron(model) else 0,
        "top_p": 0.9,
        "max_tokens": client["max_tokens"],
        "stream": False,
    }
    if _is_nemotron(model):
        body["chat_template_kwargs"] = {"enable_thinking": False}

    url = f"{client['base_url']}/chat/completions"
    headers = {
        "Authorization": f"Bearer {client['api_key']}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        url,
        headers=headers,
        json=body,
        timeout=client["timeout"],
        verify=client["verify"],
    )
    if resp.status_code == 400 and "chat_template_kwargs" in body:
        body.pop("chat_template_kwargs", None)
        resp = requests.post(
            url,
            headers=headers,
            json=body,
            timeout=client["timeout"],
            verify=client["verify"],
        )
    if resp.status_code >= 400:
        detail = (resp.text or "").strip().replace("\n", " ")
        raise RuntimeError(f"NVIDIA API {resp.status_code} for {model}: {detail[:400]}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"NVIDIA API returned non-JSON for {model}") from exc
    return _extract_message_text(payload)


def _call_intelligence_model(client, system_prompt, user_payload):
    last_error = None
    for model in _model_chain():
        try:
            text = _nvidia_chat(client, model, system_prompt, user_payload)
            return _parse_json_response(text), model
        except Exception as exc:
            last_error = exc
            if not _should_try_next_model(exc):
                raise
            print(f"  [intelligence] {model} failed — trying next NVIDIA model ...")
            time.sleep(0.8)
    raise RuntimeError(
        friendly_intelligence_error(
            last_error or RuntimeError("All NVIDIA Intelligence models failed")
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


def _article_payload(article):
    return {
        "id": article["id"],
        "title": article.get("title") or "",
        "date": article.get("pub_date") or "",
        "source": article.get("source") or "",
        "summary": article.get("summary") or article.get("subtitle") or "",
        "url": article.get("resolved_url") or article.get("url") or "",
        "related_ids": article.get("related_ids") or [],
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
    consolidated = consolidate_articles(articles)
    payloads = [_article_payload(a) for a in consolidated]
    client = _get_client()
    batch = _batch_size()
    partials = []
    models = []

    for index in range(0, len(payloads), batch):
        chunk = payloads[index:index + batch]
        user_payload = {
            "sector": sector,
            "sector_label": label,
            "start_date": start_date,
            "end_date": end_date,
            "article_count": len(chunk),
            "articles": chunk,
        }
        report, model = _call_intelligence_model(client, SYSTEM_PROMPT, user_payload)
        partials.append(report)
        models.append(model)
        if index + batch < len(payloads):
            time.sleep(1.0)

    final = _merge_partial_reports(partials) if len(partials) > 1 else partials[0]
    return final, consolidated, models[-1] if models else None


def report_to_markdown(sector, start_date, end_date, report, articles=None):
    label = SECTOR_LABELS.get(sector, sector)
    lines = [
        f"# Redseer Intelligence Hub — {label}",
        f"**Period:** {start_date} → {end_date}",
        "",
        "## Executive Summary",
        report.get("executive_summary") or "_No summary available._",
        "",
    ]

    def section(title, rows, fields):
        lines.append(f"## {title}")
        if not rows:
            lines.append("_None identified._")
            lines.append("")
            return
        for row in rows:
            bits = [f"**{row.get(fields[0], '')}**"]
            for field in fields[1:]:
                val = row.get(field)
                if val:
                    bits.append(f"{field.replace('_', ' ')}: {val}")
            source_ids = row.get("source_ids") or []
            if source_ids:
                bits.append(f"sources: {', '.join(str(i) for i in source_ids)}")
            lines.append("- " + " | ".join(bits))
        lines.append("")

    section("Key Metrics", report.get("key_metrics"), ["company", "metric", "value", "comparison", "period"])
    section("Strategic Moves", report.get("strategic_moves"), ["company", "move", "importance"])
    section("Funding", report.get("funding"), ["company", "amount", "investor"])
    section("Partnerships", report.get("partnerships"), ["company_a", "company_b", "purpose"])
    section("Leadership Changes", report.get("leadership_changes"), ["company", "executive", "role", "change"])
    section("Product Launches", report.get("product_launches"), ["company", "product", "description"])
    section("Regulations", report.get("regulations"), ["authority", "policy", "impact"])
    section("Consumer Trends", report.get("consumer_trends"), ["trend", "evidence", "impact"])
    section("Technology & AI", report.get("technology_ai"), ["company", "technology", "business_impact"])
    section("Competitive Landscape", report.get("competitive_landscape"), ["company", "development", "implication"])

    lines.append("## What to Watch")
    watch = report.get("what_to_watch") or []
    if not watch:
        lines.append("_None identified._")
    else:
        for item in watch:
            lines.append(f"- {item}")
    lines.append("")

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
    if include_json:
        data["report"] = json.loads(data.get("report_json") or "{}")
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
    return [dict(row) for row in rows]


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
            "NVIDIA_API_KEY is not configured. "
            "Intelligence Hub uses the NVIDIA key; news processing still uses GEMINIAPIKEY."
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
    return report_row
