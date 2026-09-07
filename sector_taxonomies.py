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
from dataclasses import dataclass


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
    priority: int = 50
    # Company mentions that ALWAYS belong to this sector (any development).
    primary_companies: tuple = ()
    # Ambiguous brand names that require sector context (e.g. Open, Slice).
    contextual_companies: tuple = ()
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
    # Use only when the brief explicitly says company coverage always wins.
    primary_overrides_exclusions: bool = False
    min_confidence: float = 0.80


VALUE_COMMERCE_TAXONOMY = SectorTaxonomy(
    sector="value_commerce",
    label="Value Commerce",
    priority=100,
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
    primary_overrides_exclusions=True,
    min_confidence=float(os.environ.get("VALUE_COMMERCE_MIN_CONFIDENCE", "0.80")),
)


E_COMMERCE_TAXONOMY = SectorTaxonomy(
    sector="e_commerce",
    label="E-commerce",
    priority=50,
    primary_companies=(
        "amazon india", "flipkart", "ondc", "jiomart", "jio mart",
        "tata neu", "firstcry", "nykaa", "purplle", "pepperfry",
        "urban ladder", "reliance digital", "croma",
    ),
    strong_phrases=(
        "online retail", "ecommerce marketplace", "e-commerce marketplace",
        "digital commerce", "omnichannel retail", "cross-border ecommerce",
        "cross border ecommerce", "marketplace advertising",
        "marketplace regulations", "e-commerce regulations",
        "ai in ecommerce", "ai in e-commerce",
    ),
    contextual_phrases=(
        "marketplace sellers", "seller ecosystem", "seller onboarding",
        "customer acquisition", "returns", "fulfilment", "fulfillment",
        "warehousing", "online marketplace", "online sellers",
    ),
    commerce_context=(
        "ecommerce", "e-commerce", "online retail", "marketplace",
        "online shopping", "digital commerce", "shopping platform",
    ),
    support_phrases=(
        "seller", "advertising", "customer", "warehouse", "returns",
        "cross-border", "regulation", "artificial intelligence",
    ),
    exclusions=(
        "quick commerce", "10-minute delivery", "instant delivery", "dark store",
        "restaurant delivery", "food delivery", "ride hailing", "ride-hailing",
        "digital lending", "payment gateway", "low-price marketplace",
        "value commerce", "social commerce", "reseller ecosystem",
    ),
)


QUICK_COMMERCE_TAXONOMY = SectorTaxonomy(
    sector="quick_commerce",
    label="Quick Commerce",
    priority=95,
    primary_companies=(
        "blinkit", "zepto", "swiggy instamart", "flipkart minutes",
        "bigbasket now", "bb now", "amazon now", "dunzo",
    ),
    strong_phrases=(
        "quick commerce", "q-commerce", "qcommerce", "instant delivery",
        "10-minute delivery", "10 minute delivery", "ten-minute delivery",
        "dark stores", "dark store", "dark warehouses", "dark warehouse",
        "express grocery delivery", "grocery delivery in minutes",
    ),
    contextual_phrases=(
        "hyperlocal delivery", "grocery delivery", "express delivery",
        "grocery logistics", "fulfilment centres", "fulfillment centers",
        "last-mile delivery", "last mile delivery",
    ),
    commerce_context=(
        "grocery", "commerce", "delivery", "orders", "retail", "shopping",
        "hyperlocal", "dark store", "minutes",
    ),
    support_phrases=(
        "delivery fleet", "warehouse", "inventory", "last mile",
        "delivery partner", "order density", "fulfilment", "fulfillment",
    ),
    exclusions=(
        "restaurant food delivery", "restaurant delivery", "cloud kitchen",
        "traditional ecommerce", "standard delivery", "scheduled delivery",
    ),
)


FOOD_DELIVERY_TAXONOMY = SectorTaxonomy(
    sector="food_delivery",
    label="Food Delivery",
    priority=90,
    primary_companies=(
        "zomato", "ondc food", "magicpin", "eatsure", "rebel foods",
        "domino's india", "dominos india", "pizza hut india", "wow momo",
        "wow! momo", "mcdonald's india", "mcdonalds india",
    ),
    contextual_companies=("swiggy", "eternal ltd", "eternal limited", "thrive"),
    strong_phrases=(
        "food delivery", "restaurant delivery", "cloud kitchen",
        "cloud kitchens", "food ordering", "online food ordering",
        "restaurant technology", "restaurant partners",
        "delivery commissions", "food logistics", "kitchen automation",
    ),
    contextual_phrases=(
        "delivery fleet", "delivery partners", "restaurant aggregator",
        "restaurant app", "restaurant orders", "meal delivery",
    ),
    commerce_context=(
        "restaurant", "food", "meal", "kitchen", "menu", "dining",
        "delivery", "order",
    ),
    support_phrases=(
        "commission", "delivery partner", "fleet", "kitchen", "order value",
        "restaurant partner",
    ),
    exclusions=(
        "grocery delivery", "quick commerce", "instant grocery",
        "dark store", "10-minute delivery",
    ),
)


FASHION_TAXONOMY = SectorTaxonomy(
    sector="fashion",
    label="Fashion",
    priority=93,
    primary_companies=(
        "myntra", "ajio", "nykaa fashion", "tata cliq", "zara india",
        "h&m india", "uniqlo india", "shoppers stop", "pantaloons",
        "max fashion", "westside", "zudio", "vishal mega mart", "v-mart",
    ),
    contextual_companies=(
        "lifestyle stores", "lifestyle", "reliance trends", "trends",
    ),
    strong_phrases=(
        "fashion retail", "fashion ecommerce", "fashion e-commerce",
        "fast fashion", "apparel demand", "fashion brands",
        "seasonal collection", "seasonal collections", "clothing brand",
        "footwear brand",
    ),
    contextual_phrases=(
        "apparel", "clothing", "accessories", "footwear", "ethnic wear",
        "western wear", "collection launch", "fashion label",
    ),
    commerce_context=(
        "fashion", "retail", "brand", "store", "shopping", "ecommerce",
        "e-commerce", "collection",
    ),
    support_phrases=(
        "store expansion", "online sales", "consumer demand", "collection",
        "brand launch", "retail sales",
    ),
    exclusions=(
        "beauty", "cosmetics", "skincare", "makeup", "haircare",
        "personal care", "fragrance",
    ),
)


BPC_TAXONOMY = SectorTaxonomy(
    sector="bpc",
    label="BPC",
    priority=92,
    primary_companies=(
        "nykaa", "purplle", "tira beauty", "smytten", "mamaearth",
        "honasa consumer", "honasa", "sugar cosmetics", "plum goodness",
        "minimalist skincare", "pilgrim beauty", "wow skin science",
        "lotus herbals", "biotique", "l'oréal india", "loreal india",
    ),
    contextual_companies=("tira", "plum", "minimalist", "pilgrim"),
    strong_phrases=(
        "beauty ecommerce", "beauty e-commerce", "d2c beauty",
        "beauty and personal care", "beauty & personal care",
        "beauty retail", "cosmetics market", "skincare market",
        "makeup brand", "personal care brand", "wellness beauty",
    ),
    contextual_phrases=(
        "beauty", "cosmetics", "skincare", "haircare", "makeup",
        "fragrance", "personal care", "grooming",
    ),
    commerce_context=(
        "brand", "retail", "ecommerce", "e-commerce", "market", "consumer",
        "product", "launch", "sales", "store", "d2c",
    ),
    support_phrases=(
        "product launch", "store expansion", "online sales", "consumer demand",
        "ingredients", "category growth",
    ),
    exclusions=(
        "pharmaceutical", "pharma", "medicine", "hospital", "clinical trial",
        "healthcare", "prescription drug",
    ),
)


E_LOGISTICS_TAXONOMY = SectorTaxonomy(
    sector="e_logistics",
    label="E-Logistics",
    priority=88,
    primary_companies=(
        "delhivery", "ecom express", "xpressbees", "xpress bees",
        "shadowfax", "ekart", "blue dart", "bluedart", "dtdc",
        "loadshare", "shiprocket", "pickrr", "india post",
    ),
    contextual_companies=("porter logistics", "porter"),
    strong_phrases=(
        "e-logistics", "logistics technology", "parcel delivery",
        "reverse logistics", "last-mile logistics", "last mile logistics",
        "courier services", "fulfilment logistics", "fulfillment logistics",
        "freight logistics", "supply chain logistics",
    ),
    contextual_phrases=(
        "logistics", "last-mile delivery", "last mile delivery", "courier",
        "parcel", "fulfilment", "fulfillment", "warehousing", "freight",
        "supply chain", "distribution",
    ),
    commerce_context=(
        "delivery", "logistics", "parcel", "courier", "warehouse", "freight",
        "supply chain", "distribution", "ecommerce", "e-commerce",
    ),
    support_phrases=(
        "delivery network", "warehouse automation", "fleet", "shipping",
        "distribution centre", "distribution center",
    ),
    exclusions=(
        "ride hailing", "ride-hailing", "passenger mobility", "bike taxi",
        "cab aggregator", "taxi service", "commuter",
    ),
)


FINTECH_TAXONOMY = SectorTaxonomy(
    sector="fintech",
    label="Fintech",
    priority=94,
    primary_companies=(
        "phonepe", "google pay india", "gpay india", "paytm", "bharatpe",
        "razorpay", "pine labs", "mobikwik", "fi money", "jupiter money",
        "groww", "zerodha", "angel one", "upstox", "policybazaar",
        "policy bazaar", "acko", "lendingkart", "kreditbee", "moneyview",
        "cashfree payments", "juspay", "perfios", "setu fintech",
    ),
    contextual_companies=(
        "cred", "navi", "slice", "cashfree", "open financial",
        "open money", "setu",
    ),
    strong_phrases=(
        # Payments
        "digital payments", "qr payments", "merchant payments",
        "payment gateway", "payment processing", "upi payments",
        # Lending / banking
        "digital lending", "msme lending", "embedded finance", "credit underwriting",
        "neobank", "neobanks", "digital banking", "open banking",
        "banking apis", "account aggregator",
        # Insurance / wealth
        "insurtech", "embedded insurance", "digital insurance",
        "wealthtech", "retail investing", "stock broking",
        # Infrastructure / regulation
        "upi lite", "upi circle", "digital rupee", "cbdc", "ocen",
        "fintech regulation", "fintech regulations", "digital kyc",
    ),
    contextual_phrases=(
        "upi", "wallet", "bnpl", "personal loans", "payment regulations",
        "mutual funds", "sip", "wealth management", "fastag",
        "npci", "banking api", "merchant acquiring",
    ),
    commerce_context=(
        "fintech", "payment", "payments", "lending", "loan", "credit",
        "banking", "insurance", "investing", "investment", "broker",
        "merchant", "financial", "finance", "rbi", "npci", "sebi",
    ),
    support_phrases=(
        "rbi", "npci", "sebi", "regulation", "compliance", "merchant",
        "transaction", "financial inclusion", "api",
    ),
    exclusions=(
        "quarterly bank earnings", "bank profit", "net interest income",
        "gross npa", "branch expansion", "cryptocurrency", "bitcoin",
        "ethereum", "crypto exchange",
    ),
    primary_overrides_exclusions=True,
)


MOBILE_ELECTRONICS_TAXONOMY = SectorTaxonomy(
    sector="mobile_electronics",
    label="Mobile & Electronics",
    priority=86,
    primary_companies=(
        "samsung india", "vivo india", "xiaomi india", "oneplus india",
        "realme india", "oppo india",
    ),
    contextual_companies=("apple india",),
    strong_phrases=(
        "smartphone market", "smartphone sales", "mobile phone market",
        "consumer electronics market", "electronics retail",
        "electronics ecommerce", "electronics e-commerce",
    ),
    contextual_phrases=(
        "smartphone", "mobile phone", "laptop", "television", "smart tv",
        "consumer electronics", "mobile handset",
    ),
    commerce_context=(
        "india", "market", "retail", "sales", "launch", "ecommerce",
        "e-commerce", "consumer", "shipment",
    ),
    support_phrases=(
        "market share", "shipments", "offline retail", "online sales",
        "festive sales", "manufacturing",
    ),
    exclusions=(
        "enterprise hardware", "data center", "semiconductor equipment",
        "telecom network", "software services",
    ),
)


CHOCOLATE_TAXONOMY = SectorTaxonomy(
    sector="chocolate",
    label="Chocolate",
    priority=91,
    primary_companies=(
        "cadbury", "cadbury dairy milk", "cadbury celebrations", "cadbury bournville",
        "mondelez india", "mondelez", "kitkat", "nestle munch", "nestle milkybar",
        "ferrero india", "ferrero rocher", "kinder joy", "kinder india",
        "fabelle", "itc fabelle", "amul chocolate", "campco", "lotte choco pie",
        "lotte chocolate", "hershey india", "theobroma", "paul & mike", "mason & co",
        "soklet", "kocoatrait", "country bean", "lotus chocolate",
        "parle chocolate", "5 star chocolate", "perk chocolate", "gems chocolate",
    ),
    contextual_companies=(
        "amul", "nestle india", "nestlé india", "itc limited", "ferrero",
        "hershey", "mars india", "snickers", "galaxy chocolate",
    ),
    strong_phrases=(
        "chocolate market", "chocolate brand", "chocolate maker",
        "chocolate manufacturing", "chocolate factory", "confectionery india",
        "indian confectionery", "cocoa processing", "cocoa grinding",
        "dairy milk", "premium chocolate", "bean to bar", "bean-to-bar",
        "chocolate launch", "chocolate sales",
    ),
    contextual_phrases=(
        "chocolate", "cocoa", "confectionery", "cocoa beans", "cocoa duty",
        "cocoa prices", "chocolate bar", "praline",
    ),
    commerce_context=(
        "chocolate", "cocoa", "confectionery", "cocoa beans", "praline",
        "dairy milk", "kitkat", "ferrero", "cadbury", "fabelle",
        "bean to bar", "cocoa grinding", "festive gifting",
    ),
    support_phrases=(
        "festive sales", "diwali gifting", "product launch", "market share",
        "price hike", "cocoa inflation", "distribution", "modern trade",
    ),
    competitors=(
        "unilever", "britannia", "parle", "itc",
    ),
    competitor_context=(
        "chocolate", "confectionery", "cocoa", "cadbury", "kitkat",
    ),
    exclusions=(
        "chocolate cake recipe", "hot chocolate weather", "hair color",
        "chocolate brown", "restaurant dessert", "bakery cafe menu",
        "food delivery", "cloud kitchen", "quick commerce",
        "pharma", "cocoa butter lotion", "skincare",
    ),
    primary_overrides_exclusions=True,
    min_confidence=0.80,
)


MEDIA_ENTERTAINMENT_TAXONOMY = SectorTaxonomy(
    sector="media_entertainment",
    label="Media & Entertainment",
    priority=89,
    primary_companies=(
        "kuku tv", "kukutv", "kuku fm", "kukufm",
        "storytv", "story tv", "story tv dailies",
        "pocket fm", "pocketfm",
        "pratilipi", "pratilipi fm",
        "rocket reels",
        "jiosaavn", "jio saavn",
        "gaana",
        "wynk music", "wynk",
        "storytel",
        "zee5 bullet", "kutting", "fatafat",
        "hoichoi sooper",
        "hungama music", "hungama digital",
        "hotstar tadka", "jiohotstar tadka",
    ),
    contextual_companies=(
        "tadka", "tadka app",
        "spotify india", "audible india", "amazon music",
        "kuku",
        "moj app", "josh app", "sharechat",
        "youtube shorts", "dramabox", "reelshort",
    ),
    strong_phrases=(
        "microdrama", "micro-drama", "micro drama",
        "short-form video", "short form video", "short-form content",
        "short drama", "vertical drama", "vertical series",
        "audio storytelling", "audiobook platform", "audio series",
        "music streaming india", "podcast platform india",
        "short-form entertainment", "bite-sized series",
    ),
    contextual_phrases=(
        "microdrama", "short drama", "audio streaming", "audiobook",
        "music streaming", "podcast", "short-form", "short form",
        "vertical video", "mini series",
    ),
    commerce_context=(
        "microdrama", "short drama", "short-form", "short form",
        "audio storytelling", "audiobook", "music streaming",
        "podcast", "vertical series", "kuku", "storytv", "pocket fm",
        "subscriber", "streaming", "app", "jiohotstar", "hotstar tadka",
    ),
    support_phrases=(
        "downloads", "paying subscribers", "content slate",
        "originals", "monetisation", "monetization", "ipo",
    ),
    competitors=(
        "spotify", "audible", "youtube", "amazon",
    ),
    competitor_context=(
        "microdrama", "short drama", "audio streaming", "audiobook",
        "kuku", "storytv", "pocket fm", "music streaming",
    ),
    exclusions=(
        "box office", "box-office", "theatrical release",
        "multiplex", "pvr inox", "pvr cinemas", "bookmyshow",
        "bollywood release", "movie release", "film review",
        "movie collection", "opening day collection", "trailer launch",
        "cbfc", "film certification", "yash raj films", "dharma productions",
        "the media reported", "according to media", "social media post",
        "tadka dal", "tadka masala", "jeera tadka", "recipe",
        "influencer marketing", "ride hailing", "food delivery",
        "fantasy sports app", "dream11",
    ),
    primary_overrides_exclusions=True,
    min_confidence=0.80,
)


TAXONOMIES = {
    taxonomy.sector: taxonomy
    for taxonomy in (
        E_COMMERCE_TAXONOMY,
        QUICK_COMMERCE_TAXONOMY,
        FOOD_DELIVERY_TAXONOMY,
        FASHION_TAXONOMY,
        BPC_TAXONOMY,
        E_LOGISTICS_TAXONOMY,
        FINTECH_TAXONOMY,
        MOBILE_ELECTRONICS_TAXONOMY,
        VALUE_COMMERCE_TAXONOMY,
        CHOCOLATE_TAXONOMY,
        MEDIA_ENTERTAINMENT_TAXONOMY,
    )
}


def classify_taxonomy(taxonomy, title, body="", subtitle=""):
    """Score an article against a sector taxonomy.

    Returns (matched, confidence, reasons). Precision-first: only strong,
    unambiguous signals cross the confidence threshold."""
    haystack = _normalize(f"{title} {subtitle} {body}")
    if not haystack.strip():
        return False, 0.0, []

    reasons = []

    exclusion_hits = _matched_phrases(haystack, taxonomy.exclusions)

    # 1. Primary companies have highest priority, but explicit adjacent-sector
    # intent can lower a broad company match unless the taxonomy says "always".
    primary_hits = _matched_phrases(haystack, taxonomy.primary_companies)
    if primary_hits:
        reasons.append(f"primary company: {', '.join(primary_hits)}")
        if exclusion_hits and not taxonomy.primary_overrides_exclusions:
            reasons.append(
                f"adjacent-sector intent: {', '.join(exclusion_hits[:3])}"
            )
            return True, 0.81, reasons
        return True, 0.95, reasons

    score = 0.0

    strong_hits = _matched_phrases(haystack, taxonomy.strong_phrases)
    has_context = bool(_matched_phrases(haystack, taxonomy.commerce_context))
    contextual_company_hits = (
        _matched_phrases(haystack, taxonomy.contextual_companies) if has_context else []
    )
    contextual_hits = (
        _matched_phrases(haystack, taxonomy.contextual_phrases) if has_context else []
    )
    n_strong = len(strong_hits) + len(contextual_hits) + len(contextual_company_hits)

    if strong_hits:
        reasons.append(f"theme: {', '.join(strong_hits[:4])}")
    if contextual_company_hits:
        reasons.append(
            f"company+sector context: {', '.join(contextual_company_hits[:3])}"
        )
    if contextual_hits:
        reasons.append(f"theme+commerce context: {', '.join(contextual_hits[:4])}")

    if contextual_company_hits:
        score = max(score, 0.91)
    if n_strong >= 2:
        score = max(score, 0.88)
    elif strong_hits:
        score = max(score, 0.85)
    elif contextual_hits:
        score = max(score, 0.82)

    # 2. Competitors count only with sector-relevant context.
    competitor_hits = _matched_phrases(haystack, taxonomy.competitors)
    if competitor_hits:
        competitor_context_hits = _matched_phrases(haystack, taxonomy.competitor_context)
        if competitor_context_hits:
            reasons.append(
                f"competitor with context: {competitor_hits[0]} ({competitor_context_hits[0]})"
            )
            # Explicit competitor strategy is strong enough to override that
            # competitor's broad sector (e.g. Flipkart price war -> Value Commerce).
            score = max(score, 0.96)
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
    if exclusion_hits and score > 0:
        reasons.append(f"exclusion penalty: {', '.join(exclusion_hits[:3])}")
        score -= 0.25

    score = max(0.0, min(score, 0.97))
    return score >= taxonomy.min_confidence, score, reasons


def classify_article_taxonomies(title, body="", subtitle=""):
    """Return ranked taxonomy matches for an article.

    The highest-confidence sector wins. Taxonomy priority breaks genuine ties
    (specialist sectors beat broad e-commerce; Value Commerce wins JioMart
    overlap). This minimizes noisy multi-sector assignment.
    """
    candidates = []
    for taxonomy in TAXONOMIES.values():
        matched, confidence, reasons = classify_taxonomy(
            taxonomy, title, body, subtitle
        )
        if matched:
            candidates.append(
                {
                    "sector": taxonomy.sector,
                    "confidence": confidence,
                    "priority": taxonomy.priority,
                    "reasons": reasons,
                }
            )
    candidates.sort(
        key=lambda item: (item["confidence"], item["priority"]),
        reverse=True,
    )
    return candidates


def best_taxonomy_sector(title, body="", subtitle=""):
    candidates = classify_article_taxonomies(title, body, subtitle)
    return candidates[0]["sector"] if candidates else None


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
    Explicit AI confidence below 80 is always dropped. Local taxonomy fallback
    is used only when an older model omits confidence entirely."""
    gated = []
    allowed_non_taxonomy = {"ride_hailing", "cross_sector"}
    for sector in sectors or []:
        taxonomy = TAXONOMIES.get(sector)
        if not taxonomy:
            if sector not in allowed_non_taxonomy:
                continue
            conf = confidences.get(sector) if isinstance(confidences, dict) else None
            # Ride Hailing keeps its existing dedicated local filter; all other
            # AI tags require the global 80% threshold.
            if sector == "ride_hailing" or (
                isinstance(conf, (int, float)) and conf >= 80
            ):
                gated.append(sector)
            continue
        threshold_pct = taxonomy.min_confidence * 100
        conf = confidences.get(sector) if isinstance(confidences, dict) else None
        if isinstance(conf, (int, float)):
            if conf >= threshold_pct:
                gated.append(sector)
            continue
        # Backward-compatible fallback only when an older model omits the
        # confidence field entirely. An explicit sub-80 score is always dropped.
        if taxonomy_matches(sector, title, body, subtitle):
            gated.append(sector)

    taxonomy_tags = [sector for sector in gated if sector in TAXONOMIES]
    non_taxonomy_tags = [sector for sector in gated if sector not in TAXONOMIES]
    if taxonomy_tags:
        # Business context and specialist priority resolve overlap before the
        # model's raw score, preventing broad e-commerce from swallowing verticals.
        def rank(sector):
            ai_conf = confidences.get(sector, 0) if isinstance(confidences, dict) else 0
            local_match, local_conf, _ = classify_taxonomy(
                TAXONOMIES[sector], title, body, subtitle
            )
            return (
                1 if local_match else 0,
                local_conf if local_match else 0,
                TAXONOMIES[sector].priority,
                ai_conf,
            )

        taxonomy_tags = [max(taxonomy_tags, key=rank)]
    return taxonomy_tags + non_taxonomy_tags


def gemini_taxonomy_rules():
    """Generate compact, complete taxonomy instructions for Gemini."""
    lines = [
        "",
        "Apply these India-focused sector taxonomies STRICTLY. Company context "
        "has highest priority; themes require business context, not incidental words:",
    ]
    for taxonomy in TAXONOMIES.values():
        companies = taxonomy.primary_companies + taxonomy.contextual_companies
        themes = taxonomy.strong_phrases + taxonomy.contextual_phrases
        lines.extend(
            [
                f"- {taxonomy.sector}: companies [{', '.join(companies)}]; "
                f"themes [{', '.join(themes)}].",
                f"  Exclude [{', '.join(taxonomy.exclusions)}]. "
                f"Minimum confidence {int(taxonomy.min_confidence * 100)}%.",
            ]
        )
    lines.extend(
        [
            "- VALUE COMMERCE SPECIAL RULE: Meesho, Shopsy, Snapdeal, and JioMart "
            "always map to value_commerce. General Amazon/Flipkart/Myntra/Ajio/ONDC "
            "news maps there only with low-price, Bharat, seller-migration, discount, "
            "social-commerce, reseller, or price-war intent.",
            "- OVERLAP RULE: return one most relevant sector whenever possible. "
            "Specialist intent overrides broad e_commerce (e.g. Flipkart Minutes -> "
            "quick_commerce; Nykaa cosmetics -> bpc; Nykaa Fashion -> fashion; "
            "Cadbury/Mondelez -> chocolate; Kuku TV/StoryTV/Tadka/Pocket FM -> "
            "media_entertainment). Do not tag media_entertainment for Bollywood "
            "movie releases, box office, or multiplex news. "
            "Use multiple sectors only when the article materially covers both.",
            '- For every tagged sector return "sector_confidence" from 0-100. '
            "Do not tag any sector below 80%; use cross_sector (Other) when the "
            "article is relevant but no sector reaches 80%.",
        ]
    )
    return "\n".join(lines) + "\n"
