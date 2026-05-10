"""
test_crawler.py
---------------
Unit tests for crawler.py and classifier.py.

Run from repo root:
    pip install pytest
    pytest test_crawler.py -v
"""

import os
import sys

import pytest
from unittest.mock import patch, MagicMock

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from crawler import (
    _get_meta,
    _extract_og_tags,
    _extract_headings,
    _extract_body_text,
    _extract_canonical,
    crawl,
)
from classifier import _classify_with_keywords, classify
from bs4 import BeautifulSoup


def _session_context_mock(response: MagicMock) -> MagicMock:
    """requests.Session() as context manager → .get() returns `response`."""
    instance = MagicMock()
    instance.get.return_value = response
    cm = MagicMock()
    cm.__enter__.return_value = instance
    cm.__exit__.return_value = False
    return cm


# ── Fixtures ──────────────────────────────────────────────────────────────────

SAMPLE_HTML = """
<html>
  <head>
    <title>Cuisinart CPT-122 Compact 2-Slice Toaster</title>
    <meta name="description" content="Best compact toaster for kitchen use.">
    <meta name="keywords" content="toaster, kitchen, cuisinart">
    <meta property="og:title" content="Cuisinart Toaster - Amazon">
    <meta property="og:type" content="product">
    <link rel="canonical" href="https://amazon.com/dp/B009GQ034C">
  </head>
  <body>
    <h1>Cuisinart CPT-122 Toaster</h1>
    <h2>Product Details</h2>
    <h2>Customer Reviews</h2>
    <h3>Top Reviews</h3>
    <p>This toaster is great for any kitchen. Wide slots, perfect toast every time.</p>
    <script>console.log("ignore me")</script>
  </body>
</html>
"""

@pytest.fixture
def soup():
    return BeautifulSoup(SAMPLE_HTML, "html.parser")


# ── crawler.py unit tests ─────────────────────────────────────────────────────

class TestGetMeta:
    def test_extracts_name_meta(self, soup):
        assert _get_meta(soup, "description") == "Best compact toaster for kitchen use."

    def test_extracts_keyword_meta(self, soup):
        assert _get_meta(soup, "keywords") == "toaster, kitchen, cuisinart"

    def test_extracts_og_property(self, soup):
        assert _get_meta(soup, "og:title") == "Cuisinart Toaster - Amazon"

    def test_returns_none_for_missing(self, soup):
        assert _get_meta(soup, "nonexistent") is None


class TestExtractOgTags:
    def test_extracts_og_tags(self, soup):
        og = _extract_og_tags(soup)
        assert og.get("og:title") == "Cuisinart Toaster - Amazon"
        assert og.get("og:type") == "product"

    def test_no_og_tags_returns_empty(self):
        empty_soup = BeautifulSoup("<html><head></head></html>", "html.parser")
        assert _extract_og_tags(empty_soup) == {}


class TestExtractHeadings:
    def test_extracts_h1(self, soup):
        headings = _extract_headings(soup)
        assert "Cuisinart CPT-122 Toaster" in headings["h1"]

    def test_extracts_h2(self, soup):
        headings = _extract_headings(soup)
        assert "Product Details" in headings["h2"]
        assert "Customer Reviews" in headings["h2"]

    def test_extracts_h3(self, soup):
        headings = _extract_headings(soup)
        assert "Top Reviews" in headings["h3"]


class TestExtractBodyText:
    def test_extracts_visible_text(self, soup):
        body = _extract_body_text(soup)
        assert "toaster" in body.lower()

    def test_excludes_script_tags(self, soup):
        body = _extract_body_text(soup)
        assert "console.log" not in body

    def test_respects_max_chars(self, soup):
        body = _extract_body_text(soup, max_chars=10)
        assert len(body) <= 10


class TestExtractCanonical:
    def test_extracts_canonical(self, soup):
        assert _extract_canonical(soup) == "https://amazon.com/dp/B009GQ034C"

    def test_returns_none_when_absent(self):
        s = BeautifulSoup("<html><head></head></html>", "html.parser")
        assert _extract_canonical(s) is None


class TestCrawl:
    def test_crawl_success(self):
        """Mock Session.get to avoid hitting real URLs in unit tests."""
        mock_response = MagicMock()
        mock_response.text = SAMPLE_HTML
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        with patch("crawler.requests.Session", return_value=_session_context_mock(mock_response)):
            result = crawl("https://amazon.com/dp/B009GQ034C")

        assert result["error"] is None
        assert result["status_code"] == 200
        assert result["title"] == "Cuisinart CPT-122 Compact 2-Slice Toaster"
        assert result["description"] == "Best compact toaster for kitchen use."
        assert "toaster" in result["keywords"]
        assert result["canonical"] == "https://amazon.com/dp/B009GQ034C"

    def test_crawl_timeout(self):
        import requests as req

        mock_response = MagicMock()
        cm = _session_context_mock(mock_response)
        cm.__enter__.return_value.get.side_effect = req.exceptions.Timeout

        with patch("crawler.requests.Session", return_value=cm):
            result = crawl("https://example.com")
        assert result["error"] is not None
        assert "timed out" in result["error"].lower()

    def test_crawl_http_error(self):
        import requests as req

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = req.exceptions.HTTPError(response=mock_resp)

        with patch("crawler.requests.Session", return_value=_session_context_mock(mock_response)):
            result = crawl("https://example.com/404")
        assert result["error"] is not None


# ── classifier.py unit tests ──────────────────────────────────────────────────

class TestKeywordClassifier:
    def test_classifies_kitchen(self):
        meta = {"title": "Best Toaster 2024", "description": "cuisinart kitchen appliance", "body_snippet": ""}
        topics = _classify_with_keywords(meta["title"], meta["description"], meta["body_snippet"])
        assert "Kitchen & Appliances" in topics

    def test_classifies_outdoor(self):
        topics = _classify_with_keywords("How to go hiking", "outdoor camping trail guide", "")
        assert "Outdoor & Camping" in topics

    def test_returns_empty_for_no_match(self):
        topics = _classify_with_keywords("xyzzy frobble nonce", "", "")
        assert topics == []

    def test_limits_to_5_topics(self):
        # text with many keyword hits
        big_text = "toaster hiking laptop stock travel recipe car yoga hotel"
        topics = _classify_with_keywords(big_text, big_text, big_text)
        assert len(topics) <= 5


class TestClassify:
    def test_classify_skips_empty_metadata(self):
        meta = {"title": "", "description": "", "body_snippet": ""}
        result = classify(meta)
        assert result["topics"] == []
        assert result["classification_source"] == "none"

    def test_classify_uses_keyword_fallback_without_api_key(self):
        meta = {
            "title": "Best Toasters for Your Kitchen",
            "description": "Compact toaster review",
            "body_snippet": "kitchen appliance toaster cuisinart",
        }
        with patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "", "GEMINI_API_KEY": ""},
            clear=False,
        ):
            result = classify(meta)
        assert result["classification_source"] == "keywords"
        assert len(result["topics"]) > 0
