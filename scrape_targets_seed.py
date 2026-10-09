"""
Default Festive Sale scrape targets: research firms, company newsrooms,
and official Instagram handles from the monitor list.
"""

# kind, label, url_or_handle
DEFAULT_WEBSITE_TARGETS = [
    # Research firms / festive barometers
    ("website", "Datum Intelligence — Festive Barometer", "https://datumintell.com/festive-barometer"),
    ("website", "Datum Intelligence — Home", "https://datumintell.com/"),
    ("website", "Unicommerce — Blog / Festive Reports", "https://unicommerce.com/blog/"),
    (
        "website",
        "Bain — How India Shops Online 2026",
        "https://www.bain.com/insights/how-india-shops-online-2026/",
    ),
    (
        "website",
        "BCG — India Connected Commerce",
        "https://www.bcg.com/publications/2026/india-clicks-and-bricks-are-defining-the-future-of-e-commerce",
    ),
    ("website", "Kantar — Inspiration / Ecommerce", "https://www.kantar.com/inspiration"),
    ("website", "GoKwik — Blog", "https://www.gokwik.co/blog/"),
    ("website", "Shiprocket — Home / Insights", "https://www.shiprocket.in/"),
    # Company newsrooms / IR / blogs
    ("website", "Flipkart Stories", "https://stories.flipkart.com/"),
    ("website", "Flipkart Stories — Newsroom", "https://stories.flipkart.com/newsroom/"),
    ("website", "Flipkart Stories — Announcements", "https://stories.flipkart.com/announcement/"),
    ("website", "About Amazon India", "https://www.aboutamazon.in/"),
    ("website", "Amazon Press Center — India", "https://press.aboutamazon.com/in"),
    ("website", "Meesho — Blog", "https://blog.meesho.com/"),
    ("website", "Meesho — Investor Relations", "https://ir.meesho.com/"),
    ("website", "Myntra — Press", "https://www.myntra.com/press"),
    ("website", "Snapdeal — Blog", "https://blog.snapdeal.com/"),
    ("website", "Reliance Industries — Home / News", "https://www.ril.com/"),
    ("website", "JioMart", "https://www.jiomart.com/"),
    ("website", "Nykaa — Investor Relations", "https://www.nykaa.com/investor-relations/lp"),
    ("website", "Eternal — Investor Relations", "https://www.eternal.com/investor-relations"),
    ("website", "Swiggy — Investor Relations", "https://www.swiggy.com/investor-relations"),
    ("website", "Swiggy Diaries", "https://blog.swiggy.com/"),
    ("website", "Swiggy Diaries — Instamart", "https://blog.swiggy.com/category/instamart/"),
    ("website", "Zepto — Press", "https://www.zeptonow.com/press"),
    ("website", "Zepto — Home / Blog", "https://www.zeptonow.com/"),
]

# Official Instagram handles matching the attached monitor list
DEFAULT_INSTAGRAM_TARGETS = [
    ("instagram", "Amazon India Instagram", "amazondotin"),
    ("instagram", "Flipkart India Instagram", "flipkart"),
    ("instagram", "Flipkart Mobiles Instagram", "flipkartmobiles"),
    ("instagram", "Flipkart Homes", "flipkarthomes"),
    ("instagram", "Flipkart Lifestyle (BPC)", "flipkartlifestyle"),
    ("instagram", "Flipkart Fashion", "flipkartfashion"),
    ("instagram", "Flipkart Beauty", "flipkartbeauty"),
    ("instagram", "Flipkart Minutes", "flipkartminutes"),
    ("instagram", "Flipkart TechSpert", "flipkarttechspert"),
    ("instagram", "Spoyl", "spoylonflipkart"),
    ("instagram", "Flipkart Video", "flipkartvideo"),
]


def all_default_targets():
    return list(DEFAULT_WEBSITE_TARGETS) + list(DEFAULT_INSTAGRAM_TARGETS)
