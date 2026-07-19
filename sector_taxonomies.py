"""
sector_taxonomies.py

Modular, precision-first sector taxonomies for AI/keyword news filtering.

Each SectorTaxonomy describes one sector via primary companies, semantic
theme phrases, competitor rules, and exclusions. classify_taxonomy() scores
an article and only accepts it above the confidence threshold (default 80%).

Add new sectors (Retail, Quick Commerce, Food Delivery, Fintech, Mobility...)
by defining another SectorTaxonomy and registering it in TAXONOMIES.
"""

import os
import re
from dataclasses import dataclass, field


def _normalize(text):
    return re.sub(r"\s+", " ", (text or "")).lower()


def _contains_phrase(haystack, phrase):
    phrase = phrase.lower().strip()
    if not phrase:
        return False
    if " " in phrase or "-" in phrase:
        return phrase in haystack
    return re.search(rf"\b{re.escape(phrase)}\b", haystack) is not None


def _matched_phrases(haystack, phrases):
    return [p for p in phrases if _contains_phrase(haystack, p)]


@dataclass(frozen=True)
class SectorTaxonomy:
    sector: str
    label: str
    # Company mentions that ALWAYS belong to this sector (any development).
    primary_companies: tuple = ()
    # Theme phrases that on their own signal this sector (semantic anchors).
    strong_phrases: tuple = ()
    # Theme phrases that count as strong only alongside commerce context.
    contextual_phrases: tuple = ()
    # Generic commerce/retail context that activates contextual_phrases.
    commerce_context: tuple = ()
    # Supporting themes (logistics, payments, tech, regulation) — boost only,
    # never sufficient by themselves.
    support_phrases: tuple = ()
    # Competitors: included ONLY with competitor_context present.
    competitors: tuple = ()
    competitor_context: tuple = ()
    # Topics excluded unless a primary company or multiple strong signals.
    exclusions: tuple = ()
    min_confidence: float = 0.80


VALUE_COMMERCE_TAXONOMY = SectorTaxonomy(
    sector="value_commerce",
    label="Value Commerce",
    primary_companies=(
        "meesho", "shopsy", "snapdeal", "jiomart", "jio mart",
    ),
    strong_phrases=(
        # Affordable / budget commerce
        "value commerce", "value-first retail", "value first retail",
        "value retail", "budget retail", "budget ecommerce", "budget e-commerce",
        "affordable shopping", "affordable ecommerce", "affordable e-commerce",
        "low-price marketplace", "low price marketplace", "low-price marketplaces",
        "discount marketplace", "everyday low pricing", "mass-market ecommerce",
        "mass market ecommerce", "affordable fashion", "affordable electronics",
        "affordable grocery", "affordable home products",
        # Bharat commerce
        "bharat commerce", "bharat consumers", "rural ecommerce", "rural e-commerce",
        "vernacular commerce", "regional language commerce", "semi-urban commerce",
        # Social commerce / reseller ecosystem
        "social commerce", "community commerce", "group buying", "live commerce",
        "influencer commerce", "whatsapp commerce", "creator commerce",
        "affiliate selling", "reseller", "reselling",
        # Flagship Indian value retailers
        "dmart", "vishal mega mart",
    ),
    contextual_phrases=(
        "tier 2", "tier 3", "tier 4", "tier-2", "tier-3", "tier-4",
        "tier ii", "tier iii", "small-town", "small town buyers",
        "rural consumers", "rural demand", "digital inclusion",
        "price-sensitive", "price sensitive", "budget-conscious", "budget conscious",
        "value-conscious", "value conscious", "lowest price", "smart shopping",
        "discount shopping", "kirana",
        "msme seller", "msme sellers", "sme seller", "sme sellers",
        "local manufacturers", "local brands", "seller onboarding",
        "seller financing", "seller incentives", "seller profitability",
        "seller commission", "seller commissions", "marketplace sellers",
    ),
    commerce_context=(
        "ecommerce", "e-commerce", "online shopping", "online retail",
        "marketplace", "online seller", "online sellers", "sellers",
        "shopping app", "retail", "commerce", "shopping", "online buyers",
    ),
    support_phrases=(
        # Promotions
        "discount", "cashback", "coupon", "flash sale", "festival sale",
        "promotional pricing", "price war",
        # Payments
        "cash on delivery", "cod", "upi", "bnpl", "wallet cashback",
        "refund", "payment success",
        # Logistics
        "low-cost logistics", "rural delivery", "reverse logistics",
        "last-mile delivery", "last mile delivery", "delivery optimization",
        "fulfilment", "fulfillment", "warehousing",
        # Technology
        "ai-powered recommendations", "pricing algorithm", "catalog management",
        "seller tools", "search improvements", "fraud detection",
        "supply chain optimization", "customer support ai",
        # Regulation
        "ondc", "gst", "consumer protection", "e-commerce regulation",
        "ecommerce regulation", "digital commerce policy", "fake sellers",
        "counterfeit", "marketplace compliance",
    ),
    competitors=(
        "amazon bazaar", "amazon", "flipkart minutes", "flipkart",
        "myntra", "ajio",
    ),
    competitor_context=(
        "low-price", "low price", "lowest price", "affordable", "budget",
        "value commerce", "value retail", "bharat", "tier 2", "tier 3", "tier-2",
        "tier-3", "rural", "seller migration", "marketplace competition",
        "discount strategy", "discounting", "price war", "pricing war",
        "social commerce", "reseller", "reselling", "value-focused",
        "mass market", "mass-market",
    ),
    exclusions=(
        "luxury", "premium electronics", "premium smartphone", "flagship phone",
        "iphone launch", "apple launch", "apple event", "tesla",
        "enterprise software", "saas", "gaming", "video game", "esports",
        "travel booking", "hotel booking", "airline", "flight tickets",
        "healthcare", "hospital", "pharma", "banking", "insurance",
        "cryptocurrency", "crypto", "bitcoin", "sensex", "nifty", "stock market",
        "food delivery", "cloud kitchen", "quick commerce", "10-minute delivery",
        "fine dining", "premium d2c",
    ),
    min_confidence=float(os.environ.get("VALUE_COMMERCE_MIN_CONFIDENCE", "0.80")),
)


TAXONOMIES = {
    VALUE_COMMERCE_TAXONOMY.sector: VALUE_COMMERCE_TAXONOMY,
}


def classify_taxonomy(taxonomy, title, body="", subtitle=""):
    """Score an article against a sector taxonomy.

    Returns (matched, confidence, reasons). Precision-first: only strong,
    unambiguous signals cross the confidence threshold."""
    haystack = _normalize(f"{title} {subtitle} {body}")
    if not haystack.strip():
        return False, 0.0, []

    reasons = []

    # 1. Primary companies always belong — exclusions do not apply.
    primary_hits = _matched_phrases(haystack, taxonomy.primary_companies)
    if primary_hits:
        reasons.append(f"primary company: {', '.join(primary_hits)}")
        return True, 0.95, reasons

    score = 0.0

    strong_hits = _matched_phrases(haystack, taxonomy.strong_phrases)
    has_context = bool(_matched_phrases(haystack, taxonomy.commerce_context))
    contextual_hits = (
        _matched_phrases(haystack, taxonomy.contextual_phrases) if has_context else []
    )
    n_strong = len(strong_hits) + len(contextual_hits)

    if strong_hits:
        reasons.append(f"theme: {', '.join(strong_hits[:4])}")
    if contextual_hits:
        reasons.append(f"theme+commerce context: {', '.join(contextual_hits[:4])}")

    if n_strong >= 2:
        score = 0.88
    elif strong_hits:
        score = 0.85
    elif contextual_hits:
        score = 0.82

    # 2. Competitors count only with sector-relevant context.
    competitor_hits = _matched_phrases(haystack, taxonomy.competitors)
    if competitor_hits:
        competitor_context_hits = _matched_phrases(haystack, taxonomy.competitor_context)
        if competitor_context_hits:
            reasons.append(
                f"competitor with context: {competitor_hits[0]} ({competitor_context_hits[0]})"
            )
            score = max(score, 0.85)
        elif n_strong == 0:
            # General competitor news (plain Amazon/Flipkart story) — not this sector.
            reasons.append(f"competitor without context: {competitor_hits[0]}")
            return False, min(score, 0.30), reasons

    # 3. Support themes only reinforce an existing strong signal.
    if score > 0:
        support_hits = _matched_phrases(haystack, taxonomy.support_phrases)
        if support_hits:
            reasons.append(f"support: {', '.join(support_hits[:3])}")
            score += 0.02 * min(len(support_hits), 3)

    # 4. Excluded verticals penalize — a single borderline signal won't survive.
    exclusion_hits = _matched_phrases(haystack, taxonomy.exclusions)
    if exclusion_hits and score > 0:
        reasons.append(f"exclusion penalty: {', '.join(exclusion_hits[:3])}")
        score -= 0.25

    score = max(0.0, min(score, 0.97))
    return score >= taxonomy.min_confidence, score, reasons


def taxonomy_matches(sector, title, body="", subtitle=""):
    taxonomy = TAXONOMIES.get(sector)
    if not taxonomy:
        return False
    matched, _, _ = classify_taxonomy(taxonomy, title, body, subtitle)
    return matched


def is_value_commerce_relevant(title, body="", subtitle=""):
    return taxonomy_matches("value_commerce", title, body, subtitle)


def gate_ai_sectors(sectors, confidences, title="", body="", subtitle=""):
    """Enforce taxonomy confidence thresholds on AI-classified sector tags.

    confidences: dict of sector -> 0-100 confidence from the AI (may be empty).
    Tags for taxonomy sectors are dropped when the AI confidence is below the
    threshold AND the local taxonomy classifier does not independently match."""
    gated = []
    for sector in sectors or []:
        taxonomy = TAXONOMIES.get(sector)
        if not taxonomy:
            gated.append(sector)
            continue
        threshold_pct = taxonomy.min_confidence * 100
        conf = confidences.get(sector) if isinstance(confidences, dict) else None
        if isinstance(conf, (int, float)) and conf >= threshold_pct:
            gated.append(sector)
            continue
        if taxonomy_matches(sector, title, body, subtitle):
            gated.append(sector)
    return gated


def gemini_taxonomy_rules():
    """Prompt block describing taxonomy sectors for the Gemini classifier."""
    t = VALUE_COMMERCE_TAXONOMY
    return f"""
For value_commerce in India, apply this taxonomy STRICTLY (precision over recall):
- ALWAYS tag value_commerce for news about: Meesho, Shopsy, Snapdeal, JioMart \
(any development — funding, IPO, results, leadership, partnerships, launches, \
sellers, AI, logistics, expansion, layoffs, strategy, pricing, categories).
- ALSO tag value_commerce for: affordable/budget/low-price e-commerce, discount \
marketplaces, everyday low pricing, mass-market e-commerce; Bharat / Tier 2-3-4 / \
rural / vernacular commerce and digital inclusion; social commerce, group buying, \
live/influencer/WhatsApp commerce, reseller ecosystems; MSME/SME marketplace \
sellers, seller onboarding/financing/commissions; price-sensitive or \
budget-conscious consumers; and logistics, payments (COD/UPI/BNPL), promotions, \
AI/technology, or regulation (ONDC, GST, marketplace compliance) SPECIFICALLY \
affecting low-price marketplaces.
- Competitors (Amazon, Amazon Bazaar, Flipkart, Flipkart Minutes, Myntra, Ajio, \
ONDC): tag value_commerce ONLY when the story is about low-price strategy, \
Bharat expansion, seller migration, discount/pricing wars, social commerce, or \
reseller ecosystems. Do NOT tag general Amazon/Flipkart news.
- Do NOT tag value_commerce for: luxury/premium retail, Apple/Tesla launches, \
enterprise software/SaaS, gaming, travel, healthcare, banking, insurance, crypto, \
generic stock-market stories, non-India retail, food delivery, or quick commerce \
(unless it involves Meesho/Shopsy or a clear value-commerce strategy).
- Confidence rule: include value_commerce ONLY if you are at least \
{int(t.min_confidence * 100)}% confident. For every article, also return a \
"sector_confidence" object mapping each tagged sector to your 0-100 confidence \
(e.g. {{"value_commerce": 90}}). If your value_commerce confidence is below \
{int(t.min_confidence * 100)}, leave it out (use cross_sector if still broadly relevant).
"""
