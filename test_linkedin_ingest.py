"""Unit tests for LinkedIn festive discovery scoring (no network)."""

from linkedin_ingest import quality_score, _looks_like_linkedin


def test_keeps_festive_gmv_insight():
    q = quality_score(
        "Flipkart Big Billion Days GMV up 40% YoY",
        "Orders and crore growth insight for festive season",
    )
    assert q["keep"] is True
    assert "flipkart" in q["platforms"]
    assert q["festive"] is True
    assert q["insight"] is True
    assert q["score"] >= 5


def test_rejects_hiring_noise():
    q = quality_score(
        "We're hiring at Flipkart for Diwali campaigns",
        "Join our team outing and office party",
    )
    assert q["keep"] is False


def test_rejects_non_platform():
    q = quality_score(
        "Festive barometer shows strong GMV growth YoY",
        "India ecommerce outlook with crore in orders",
    )
    assert q["keep"] is False  # no Flipkart/Amazon/Meesho/Myntra/Ajio


def test_linkedin_url_detection():
    assert _looks_like_linkedin("https://www.linkedin.com/posts/abc", "Title", "")
    assert _looks_like_linkedin("https://news.google.com/rss/...", "Story - LinkedIn", "")
    assert not _looks_like_linkedin("https://example.com/news", "Generic retail sale", "")
