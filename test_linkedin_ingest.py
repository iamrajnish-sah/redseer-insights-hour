"""Unit tests for LinkedIn festive discovery scoring + freshness (no network)."""

from datetime import date, timedelta

import linkedin_ingest
from linkedin_ingest import quality_score, _looks_like_linkedin, _is_fresh, _max_age_days


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
    assert q["keep"] is False


def test_relax_keeps_platform_festive_without_metrics():
    q = quality_score(
        "Amazon Great Indian Festival is live — festive season kickoff",
        "Team notes on shopping festival plans",
        relax=True,
    )
    assert q["keep"] is True


def test_linkedin_url_detection():
    assert _looks_like_linkedin("https://www.linkedin.com/posts/abc", "Title", "")
    assert _looks_like_linkedin("https://news.google.com/rss/...", "Story - LinkedIn", "")
    assert not _looks_like_linkedin("https://example.com/news", "Generic retail sale", "")


def test_freshness_window():
    today = date.today().isoformat()
    old = (date.today() - timedelta(days=_max_age_days() + 5)).isoformat()
    assert _is_fresh(today) is True
    assert _is_fresh(old) is False
    assert _is_fresh("") is False


def test_status_never_uses_news_apis():
    status = linkedin_ingest.status_summary()
    assert status["uses_gnews_api"] is False
    assert status["uses_newsapi"] is False
    assert "max_age_days" in status
