"""
classify_and_summarize.py  (Gemini version)

Uses the Gemini API's free tier to, for each article:
  1. Decide if it's relevant to e-commerce / quick-commerce / ride-hailing /
     value-commerce in India (or news that materially affects those sectors)
  2. Tag it with the relevant sector(s)
  3. Write a 2-3 line summary

Batches several articles into ONE prompt per call and asks for structured
JSON back, to stay well within free-tier rate limits.

When one model hits its free-tier quota or rate limit, automatically tries
the next model in MODEL_FALLBACK_CHAIN (each model has its own quota).

Requires: GEMINIAPIKEY environment variable to be set.
Get a free key at https://aistudio.google.com/apikey
"""

import os
import re
import ssl
import json
import time
import certifi
import database
from google import genai
from google.genai import types

# Tried in order. Each model has its own quota on the same API key.
# Older models (e.g. gemini-1.5-flash) may return 404 — they are omitted here.
# Override with comma-separated GEMINI_MODELS env var if needed.
DEFAULT_MODEL_FALLBACK_CHAIN = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-3-flash-preview",
    "gemini-2.5-pro",
]


def _model_fallback_chain():
    custom = os.environ.get("GEMINI_MODELS", "").strip()
    if custom:
        return [m.strip() for m in custom.split(",") if m.strip()]
    return DEFAULT_MODEL_FALLBACK_CHAIN

from sector_keywords import SECTOR_LABELS, GEMINI_SECTORS
from sector_taxonomies import gemini_taxonomy_rules, gate_ai_sectors

SECTORS = GEMINI_SECTORS

SYSTEM_PROMPT = f"""You are a news analyst for an Indian company tracking commerce and \
consumer sectors: e-commerce, quick commerce, ride hailing, value commerce, food delivery, \
fashion, beauty & personal care (BPC), e-logistics, mobile & electronics, and cross-sector \
macro/indirect impacts.

For ride hailing in India, ONLY tag ride_hailing when the story is about cab/bike/auto \
aggregators (Ola, Uber India, Rapido, Namma Yatri, BluSmart, inDrive, Meru, etc.), their \
regulation/pricing/driver partners, OR urban events that plausibly change ride demand \
(elections, exams, concerts, IPL, metro/transport strikes or disruption in major cities). \
Do NOT tag ride_hailing for Ola Electric/scooters, generic mobility, or unrelated "Ola" mentions.
{gemini_taxonomy_rules()}
For each article given, decide:
1. is_relevant: true if the article is about, or materially affects, any of these \
sectors in India (funding, competition, regulation, macro factors, key executives, \
market entry/exit, technology shifts, etc). General news with no plausible link is NOT relevant.
2. sectors: a list from {SECTORS} that apply (empty list if not relevant). Use \
"cross_sector" for macro or indirect news (e.g. RBI policy, fuel prices, labour law, GST) \
that affects multiple sectors but is not one specific category.
3. sector_confidence: an object mapping each tagged sector to your 0-100 confidence.
4. summary: a crisp 2-3 sentence summary IN YOUR OWN WORDS (only if relevant; \
empty string otherwise).

Return ONLY a JSON array, one object per input article, in the same order, with \
keys: id, is_relevant, sectors, sector_confidence, summary. No preamble, no markdown fences."""


def _get_client():
    api_key = os.environ.get("GEMINIAPIKEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINIAPIKEY environment variable is not set.")

    if os.environ.get("GEMINI_SSL_VERIFY", "").lower() in ("0", "false", "no"):
        verify = False
    else:
        cafile = (
            os.environ.get("SSL_CERT_FILE")
            or os.environ.get("REQUESTS_CA_BUNDLE")
            or certifi.where()
        )
        verify = ssl.create_default_context(cafile=cafile)

    http_options = types.HttpOptions(
        client_args={"verify": verify},
        async_client_args={"verify": verify},
    )
    return genai.Client(api_key=api_key, http_options=http_options)


def _is_ssl_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "certificate" in msg or "ssl" in msg


def _is_rate_or_quota_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(
        token in msg
        for token in ("429", "resource_exhausted", "quota", "rate limit", "rate_limit")
    )


def _should_try_next_model(exc: Exception) -> bool:
    if _is_rate_or_quota_error(exc):
        return True
    msg = str(exc).lower()
    return any(
        token in msg
        for token in ("404", "not_found", "not found", "is not supported", "is not found")
    )


def _model_error_reason(exc: Exception) -> str:
    if _is_rate_or_quota_error(exc):
        return "quota/rate limit hit"
    if _should_try_next_model(exc):
        return "not available on your API key"
    return "error"


def _parse_retry_seconds(exc: Exception):
    msg = str(exc)
    for pattern in (
        r"retry(?: after| in|delay)[:\s'\"]*(\d+)",
        r"'retryDelay':\s*'(\d+)s'",
        r"retry in (\d+\.?\d*)s",
    ):
        match = re.search(pattern, msg, re.I)
        if match:
            return float(match.group(1))
    return None


def _classification_settings():
    """Tune via env vars to control Gemini usage."""
    max_articles = os.environ.get("GEMINI_MAX_ARTICLES", "").strip()
    if not max_articles:
        max_articles = "15" if os.environ.get("VERCEL") else "80"
    max_days = os.environ.get("GEMINI_MAX_DAYS", "7").strip()
    origins = os.environ.get("GEMINI_ORIGINS", "epub,newspaper").strip()

    return {
        "limit": int(max_articles) if max_articles else None,
        "max_days": int(max_days) if max_days else None,
        "origins": [o.strip() for o in origins.split(",") if o.strip()] or None,
        "body_chars": int(os.environ.get("GEMINI_BODY_CHARS", "500")),
    }


def batch_settings():
    """Smaller batches on Vercel to stay within the 60s function timeout."""
    if os.environ.get("VERCEL"):
        return {"batch_size": 5, "pause_between_calls": 1.0}
    return {
        "batch_size": int(os.environ.get("GEMINI_BATCH_SIZE", "8")),
        "pause_between_calls": float(os.environ.get("GEMINI_BATCH_PAUSE", "2.0")),
    }


def _select_unprocessed_rows():
    settings = _classification_settings()
    rows = database.get_unprocessed(
        limit=settings["limit"],
        max_days=settings["max_days"],
        origins=settings["origins"],
    )
    pending_by_origin = database.get_unprocessed_stats(
        max_days=settings["max_days"],
        origins=settings["origins"],
    )
    total_pending = sum(pending_by_origin.values())
    return rows, settings, pending_by_origin, total_pending


def get_rows_for_processing():
    """Pick which unprocessed articles to send to Gemini (respects env limits)."""
    return _select_unprocessed_rows()


def _parse_batch_response(text):
    text = text.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


def _call_model(client, model, user_msg):
    resp = client.models.generate_content(
        model=model,
        contents=[SYSTEM_PROMPT, user_msg],
        config={"response_mime_type": "application/json"},
    )
    return _parse_batch_response(resp.text)


def _generate_batch(client, user_msg, start_model_index=0):
    """Try models in fallback chain. Returns (parsed_json, model_used, model_index)."""
    models = _model_fallback_chain()
    last_error = None

    for idx in range(start_model_index, len(models)):
        model = models[idx]
        for attempt in range(2):
            try:
                parsed = _call_model(client, model, user_msg)
                return parsed, model, idx
            except json.JSONDecodeError as e:
                raise RuntimeError(f"{model} returned invalid JSON: {e}") from e
            except Exception as e:
                last_error = e
                if not _should_try_next_model(e):
                    raise

                reason = _model_error_reason(e)
                retry_s = _parse_retry_seconds(e)
                if reason == "quota/rate limit hit" and attempt == 0 and retry_s is not None:
                    wait = min(retry_s + 1, 60)
                    print(f"  [info] {model} rate limited — waiting {wait:.0f}s ...")
                    time.sleep(wait)
                    continue

                print(f"  [info] {model} {reason} — trying next model ...")
                break

    raise last_error or RuntimeError("All Gemini models exhausted for this batch")


def classify_batch(rows, batch_size=8, pause_between_calls=2.0, body_chars=500):
    """rows: sqlite Row objects with id, title, subtitle, body.
    Returns (results dict, stats dict)."""
    client = _get_client()
    results = {}
    model_index = 0
    models_used = []
    batches_failed = 0
    ssl_help_printed = False
    quota_help_printed = False

    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        articles_payload = [
            {
                "id": r["id"],
                "title": r["title"],
                "subtitle": r["subtitle"],
                "body": (r["body"] or "")[:body_chars],
            }
            for r in batch
        ]

        user_msg = json.dumps(articles_payload, ensure_ascii=False)

        try:
            parsed, model_used, model_index = _generate_batch(client, user_msg, model_index)
            if model_used not in models_used:
                models_used.append(model_used)
                print(f"  [info] Using model: {model_used}")

            row_by_id = {r["id"]: r for r in batch}
            for item in parsed:
                row = row_by_id.get(item.get("id"))
                sectors = item.get("sectors", []) or []
                gated = gate_ai_sectors(
                    sectors,
                    item.get("sector_confidence") or {},
                    title=row["title"] if row else "",
                    body=(row["body"] or "") if row else "",
                    subtitle=(row["subtitle"] or "") if row else "",
                )
                if gated != sectors:
                    dropped = [s for s in sectors if s not in gated]
                    print(
                        f"  [taxonomy] article {item.get('id')}: dropped low-confidence "
                        f"tag(s) {dropped}"
                    )
                    # Still broadly relevant but no confident sector -> Other bucket.
                    if not gated and item.get("is_relevant"):
                        gated = ["cross_sector"]
                results[item["id"]] = {
                    "is_relevant": item.get("is_relevant", False),
                    "sectors": gated,
                    "summary": item.get("summary", ""),
                }

        except Exception as e:
            batches_failed += 1
            print(f"  [warning] batch {i}-{i + len(batch)} failed on all models: {e}")
            if _is_ssl_error(e) and not ssl_help_printed:
                ssl_help_printed = True
                print(
                    "  [help] SSL certificate error — often caused by office Wi-Fi, VPN, or antivirus.\n"
                    "         Try: (1) home/mobile hotspot, (2) pause HTTPS scanning in antivirus,\n"
                    "         or (3) before starting the server run: $env:GEMINI_SSL_VERIFY = \"false\""
                )
            elif _is_rate_or_quota_error(e) and not quota_help_printed:
                quota_help_printed = True
                print(
                    "  [help] All models hit today's free quota. Stop for now and try again tomorrow,\n"
                    "         or run Process with Gemini once per day to chip away at PENDING."
                )

        if i + batch_size < len(rows):
            time.sleep(pause_between_calls)

    stats = {
        "models_used": models_used,
        "batches_failed": batches_failed,
        "active_model": models_used[-1] if models_used else None,
    }
    return results, stats


if __name__ == "__main__":
    database.init_db()
    rows, settings, pending_by_origin, total_pending = get_rows_for_processing()
    print(f"{len(rows)} articles selected for processing ({total_pending} pending in scope).")
    if pending_by_origin:
        print("Pending by source:", ", ".join(f"{k}={v}" for k, v in pending_by_origin.items()))
    if not rows:
        exit()

    results, stats = classify_batch(rows, body_chars=settings["body_chars"])
    for row in rows:
        r = results.get(row["id"])
        if not r:
            continue
        database.update_classification(row["id"], r["is_relevant"], r["sectors"], r["summary"])

    relevant_count = sum(1 for r in results.values() if r["is_relevant"])
    print(f"Classified {len(results)} articles, {relevant_count} marked relevant.")
    if stats["models_used"]:
        print(f"Models used: {', '.join(stats['models_used'])}")
