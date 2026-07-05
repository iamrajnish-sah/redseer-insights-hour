"""
sector_keywords.py

Shared sector definitions, keyword lists, and NewsAPI queries for RSS/NewsAPI filtering.
Edit SECTOR_KEYWORDS to add company names or topics you care about.
"""

import re

SECTOR_LABELS = {
    "e_commerce": "E-commerce",
    "quick_commerce": "Quick Commerce",
    "ride_hailing": "Ride Hailing",
    "value_commerce": "Value Commerce",
    "food_delivery": "Food Delivery",
    "fashion": "Fashion",
    "bpc": "BPC",
    "e_logistics": "E-Logistics",
    "mobile_electronics": "Mobile & Electronics",
    "cross_sector": "Cross-Sector / Indirect",
}

# Old articles may still use this tag — mapped to cross_sector in the UI
LEGACY_SECTOR_ALIASES = {
    "other_relevant": "cross_sector",
}

# ── Ride hailing intelligence (India) ─────────────────────────────────────
# Direct: apps & industry terms you track
RIDE_HAILING_DIRECT = [
    "rapido",
    "namma yatri",
    "blusmart",
    "meru cabs",
    "meru taxi",
    "indrive",
    "in drive",
    "savaari",
    "uber india",
    "ola cabs",
    "ola cab",
    "ola taxi",
    "ola bike",
    "ola auto",
    "ola app",
    "ride-hailing",
    "ride hailing",
    "cab aggregator",
    "taxi aggregator",
    "bike taxi",
    "bike-taxi",
    "auto aggregator",
    "two-wheeler taxi",
    "driver partner",
    "cab fare",
    "taxi fare",
    "surge pricing",
    "cab strike",
    "auto strike",
]

# Demand drivers: events that typically lift ride volume in operational cities
RIDE_HAILING_DEMAND = [
    "concert",
    "music festival",
    "ipl",
    "cricket match",
    "t20",
    "world cup match",
    "election",
    "poll rally",
    "voting day",
    "board exam",
    "jee exam",
    "neet exam",
    "gate exam",
    "university exam",
    "exam centre",
    "metro strike",
    "metro shutdown",
    "metro suspension",
    "metro services hit",
    "metro delay",
    "metro disruption",
    "bus strike",
    "transport strike",
    "public transport",
    "railway strike",
    "train cancellation",
    "airport chaos",
    "flight delay",
    "traffic jam",
    "road closure",
    "flyover closure",
    "odd-even",
    "marathon",
    "half marathon",
    "new year eve",
    "diwali rush",
    "holi festival",
    "festival crowd",
    "rain flooding",
    "waterlogging",
]

# Cities / regions where ride-hailing apps are typically operational
RIDE_HAILING_CITIES = [
    "delhi",
    "ncr",
    "gurugram",
    "gurgaon",
    "noida",
    "faridabad",
    "ghaziabad",
    "mumbai",
    "bengaluru",
    "bangalore",
    "hyderabad",
    "chennai",
    "kolkata",
    "pune",
    "ahmedabad",
    "jaipur",
    "lucknow",
    "kochi",
    "indore",
    "chandigarh",
    "bhopal",
    "nagpur",
    "surat",
    "visakhapatnam",
    "vizag",
]

# Mobility context — pairs with demand signals when no city is named
RIDE_HAILING_MOBILITY_CONTEXT = [
    "metro",
    "commute",
    "cab demand",
    "ride demand",
    "taxi demand",
    "cab ",
    "taxi ",
    "ride share",
    "rideshare",
    "last mile",
    "airport taxi",
    "airport cab",
]

# Stories that often false-match "ola" / mobility but are not ride-hailing ops news
RIDE_HAILING_EXCLUDE = [
    "ola electric",
    "ola scooter",
    "ola s1",
    "ola s1 pro",
    "ola krutrim",
    "ola money",
    "ola foundation",
    "ola futurefactory",
    "ola cell",
    "electric scooter",
    "ev scooter",
]

# Direct sector keywords (company names, sector terms)
SECTOR_KEYWORDS = {
    "e_commerce": [
        "e-commerce", "ecommerce", "online retail", "marketplace", "d2c",
        "flipkart", "amazon india", "myntra", "meesho", "nykaa", "tata cliq",
        "snapdeal", "ajio", "jiomart", "shiprocket", "online seller",
    ],
    "quick_commerce": [
        "quick commerce", "q-commerce", "qcommerce", "quick delivery",
        "10-minute", "10 minute delivery", "dark store", "hyperlocal delivery",
        "blinkit", "zepto", "instamart", "swiggy instamart", "bigbasket", "grofers",
    ],
    "ride_hailing": [],  # matched via is_ride_hailing_relevant() — see below
    "value_commerce": [
        "value commerce", "value retail", "budget retail",
        "dmart", "reliance retail", "vishal mega mart", "spencer's",
        "d2c brand", "direct to consumer", "kirana", "bharat", "tier 2 retail",
    ],
    "food_delivery": [
        "food delivery", "foodtech", "online food ordering", "restaurant aggregator",
        "cloud kitchen", "zomato", "swiggy food", "eatfit", "rebel foods",
        "food ordering app", "qsr delivery", "dine-in delivery",
    ],
    "fashion": [
        "fashion retail", "apparel", "clothing brand", "fast fashion",
        "lifestyle retail", "ethnic wear", "footwear brand", "fashion ecommerce",
        "h&m india", "zara india", "fabindia", "manyavar", "pepe jeans",
    ],
    "bpc": [
        "beauty", "personal care", "bpc", "skincare", "cosmetics", "grooming",
        "mamaearth", "sugar cosmetics", "lakme", "himalaya wellness", "forest essentials",
        "colorbar", "plum", "boAt personal care", "hygiene products",
    ],
    "e_logistics": [
        "e-logistics", "logistics startup", "last mile delivery", "fulfillment",
        "warehouse", "supply chain", "delhivery", "bluedart", "shadowfax",
        "xpressbees", "porter", "elasticrun", "loadshare", "shiprocket logistics",
        "third party logistics", "3pl", "courier",
    ],
    "mobile_electronics": [
        "smartphone", "mobile phone", "electronics retail", "consumer electronics",
        "croma", "reliance digital", "vivo india", "samsung india", "apple india",
        "xiaomi", "oneplus", "realme", "electronics ecommerce", "gadgets",
        "laptop india", "tv retail", "iphone india",
    ],
}

GEMINI_SECTORS = list(SECTOR_KEYWORDS.keys()) + ["cross_sector"]

# Macro / indirect news (tagged cross_sector)
INDIRECT_KEYWORDS = [
    "rbi", "repo rate", "interest rate", "fuel price", "diesel price",
    "gst", "fdi retail", "consumer inflation", "fmcg", "urban consumption",
    "labour code", "gig worker", "platform worker", "digital payments",
    "upi", "consumer spending", "retail inflation", "logistics cost",
    "cold chain", "foreign direct investment", "monsoon impact retail",
    "rural demand", "credit growth",
]

NEWSAPI_QUERIES = {
    "e_commerce": "e-commerce India OR Flipkart OR Amazon India OR Myntra OR Meesho",
    "quick_commerce": "quick commerce India OR Blinkit OR Zepto OR Swiggy Instamart OR BigBasket",
    "ride_hailing": (
        '(Ola OR Rapido OR "Namma Yatri" OR BluSmart OR inDrive OR "Uber India") '
        "AND (India OR cab OR taxi OR ride OR driver OR fare) OR "
        '("metro strike" OR "metro shutdown" OR "transport strike" OR "board exam" '
        'OR election OR concert OR IPL) AND (Delhi OR Mumbai OR Bengaluru OR Gurugram OR Noida OR India)'
    ),
    "value_commerce": "value retail India OR DMart OR Reliance Retail OR budget retail India",
    "food_delivery": "food delivery India OR Zomato OR Swiggy OR cloud kitchen India",
    "fashion": "fashion retail India OR apparel ecommerce India OR Myntra fashion",
    "bpc": "beauty personal care India OR skincare India OR cosmetics startup India",
    "e_logistics": "logistics India OR Delhivery OR last mile delivery India OR Shiprocket",
    "mobile_electronics": "smartphone India OR electronics retail India OR Samsung India OR Apple India",
}


def normalize_sector_tags(sectors):
    normalized = []
    for sector in sectors or []:
        sector = LEGACY_SECTOR_ALIASES.get(sector, sector)
        if sector not in normalized:
            normalized.append(sector)
    return normalized


def _normalize(text):
    return re.sub(r"\s+", " ", (text or "")).lower()


def _contains_phrase(haystack, phrase):
    phrase = phrase.lower().strip()
    if not phrase:
        return False
    if " " in phrase or "-" in phrase:
        return phrase in haystack
    return re.search(rf"\b{re.escape(phrase)}\b", haystack) is not None


def _any_phrase(haystack, phrases):
    return any(_contains_phrase(haystack, p) for p in phrases)


def _has_ola_ride_context(haystack):
    if not _contains_phrase(haystack, "ola"):
        return False
    if _any_phrase(haystack, RIDE_HAILING_EXCLUDE):
        ride_terms = [
            "cab", "taxi", "ride", "driver", "aggregator", "fare", "auto ",
            "bike taxi", "hailing", "partner", "strike",
        ]
        return any(t in haystack for t in ride_terms)
    return True


def _has_uber_ride_context(haystack):
    if _contains_phrase(haystack, "uber india"):
        return True
    if not _contains_phrase(haystack, "uber"):
        return False
    ride_terms = [
        "cab", "taxi", "ride", "driver", "partner", "fare", "hailing",
        "aggregator", " india", "delhi", "mumbai", "bengaluru", "bangalore",
        "hyderabad", "chennai", "pune", "gurugram", "gurgaon", "ncr",
    ]
    return any(t in haystack for t in ride_terms)


def is_ride_hailing_relevant(title, body="", subtitle=""):
    """True when news affects Indian ride-hailing ops or urban ride demand."""
    haystack = _normalize(f"{title} {subtitle} {body}")

    if _any_phrase(haystack, RIDE_HAILING_DIRECT):
        if _any_phrase(haystack, RIDE_HAILING_EXCLUDE) and not (
            _has_ola_ride_context(haystack) or _has_uber_ride_context(haystack)
        ):
            return False
        return True

    if _has_ola_ride_context(haystack) or _has_uber_ride_context(haystack):
        return True

    has_demand = _any_phrase(haystack, RIDE_HAILING_DEMAND)
    has_city = _any_phrase(haystack, RIDE_HAILING_CITIES)
    has_mobility = _any_phrase(haystack, RIDE_HAILING_MOBILITY_CONTEXT)
    if has_demand and (has_city or has_mobility):
        return True

    return False


def match_sectors(title, body="", subtitle=""):
    """Return sector tags whose keywords appear in the headline or summary."""
    haystack = _normalize(f"{title} {subtitle} {body}")
    matched = []
    for sector, keywords in SECTOR_KEYWORDS.items():
        if sector == "ride_hailing":
            if is_ride_hailing_relevant(title, body, subtitle):
                matched.append("ride_hailing")
            continue
        for kw in keywords:
            if kw.lower() in haystack:
                matched.append(sector)
                break
    for kw in INDIRECT_KEYWORDS:
        if kw.lower() in haystack:
            if "cross_sector" not in matched:
                matched.append("cross_sector")
            break
    return matched


def make_summary(title, body, max_len=280):
    text = re.sub(r"<[^<]+?>", "", body or "") or title
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."
