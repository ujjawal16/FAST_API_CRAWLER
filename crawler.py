"""
crawler.py
----------
Responsible for fetching a URL and extracting structured metadata from its HTML.

Design decisions:
- Uses `requests` for HTTP with a realistic browser User-Agent to avoid bot blocks.
- Uses `BeautifulSoup` with the 'html.parser' (stdlib, no extra install) for parsing.
- Extracts: title, meta description, meta keywords, Open Graph tags, canonical URL,
  headings (h1-h3), and a clean body text snippet.
- Keeps each concern (fetch / parse / clean) in its own function for testability.
"""

import os
import re
import requests
from bs4 import BeautifulSoup
from typing import Optional
from urllib.parse import urlparse, urlunparse


# ── Constants ────────────────────────────────────────────────────────────────

def _request_timeout_sec() -> float:
    """Read timeout (seconds) for the response body; set CRAWL_TIMEOUT_SEC."""
    return float(os.environ.get("CRAWL_TIMEOUT_SEC", "60"))


def _connect_timeout_sec() -> float:
    """TLS + TCP connect cap; set CRAWL_CONNECT_TIMEOUT_SEC (default min(20, read/3))."""
    read = _request_timeout_sec()
    return float(os.environ.get("CRAWL_CONNECT_TIMEOUT_SEC", str(min(20.0, read / 3))))


def _timeout() -> tuple[float, float]:
    """(connect, read) — avoids hanging forever on connect vs slow body separately."""
    return (_connect_timeout_sec(), _request_timeout_sec())


def _normalize_crawl_url(url: str) -> str:
    """
    Upgrade http→https for public hosts. Many CDNs (e.g. retail) stall or loop on plain HTTP.
    Disable: export CRAWL_NO_HTTPS_UPGRADE=1
    """
    if os.environ.get("CRAWL_NO_HTTPS_UPGRADE"):
        return url
    p = urlparse(url)
    if p.scheme != "http":
        return url
    host = (p.hostname or "").lower()
    if host in ("localhost", "127.0.0.1") or host.startswith("192.168."):
        return url
    return urlunparse(p._replace(scheme="https"))


def _browser_headers() -> dict[str, str]:
    """Chrome-like headers; many WAFs drop minimal clients that omit Sec-Fetch-* / client hints."""
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    return {
        "User-Agent": ua,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        # gzip/deflate only — avoids needing brotli for "br"
        "Accept-Encoding": "gzip, deflate",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
        "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }

BODY_SNIPPET_MAX_CHARS = 1500  # enough context for the classifier, not too expensive


# ── Internal helpers ─────────────────────────────────────────────────────────

def _fetch_html(url: str) -> tuple[str, int]:
    """
    Fetch raw HTML for a URL.
    Returns (html_text, status_code).
    Raises requests.RequestException on network error.
    Retries once on timeout (many storefronts / CDNs are slow on cold connections).
    """
    timeout = _timeout()
    headers = _browser_headers()
    for attempt in range(2):
        try:
            with requests.Session() as session:
                session.headers.update(headers)
                response = session.get(url, timeout=timeout, allow_redirects=True)
            response.raise_for_status()
            return response.text, response.status_code
        except requests.exceptions.Timeout:
            if attempt == 0:
                continue
            raise


def _get_meta(soup: BeautifulSoup, name: str) -> Optional[str]:
    """
    Generic helper to pull a <meta name="..."> or <meta property="..."> content value.
    Handles both standard meta tags and Open Graph / Twitter Card variants.
    """
    tag = soup.find("meta", attrs={"name": name}) or \
          soup.find("meta", attrs={"property": name})
    if tag:
        return tag.get("content", "").strip() or None
    return None


def _extract_og_tags(soup: BeautifulSoup) -> dict:
    """
    Pull all Open Graph (og:*) and Twitter Card (twitter:*) tags into a dict.
    These often carry richer data than standard meta tags.
    Example: og:type = "product", og:image, twitter:card, etc.
    """
    og = {}
    for tag in soup.find_all("meta"):
        prop = tag.get("property", "") or tag.get("name", "")
        if prop.startswith("og:") or prop.startswith("twitter:"):
            content = tag.get("content", "").strip()
            if prop and content:
                og[prop] = content
    return og


def _extract_headings(soup: BeautifulSoup) -> dict:
    """
    Collect all h1, h2, h3 text — they are strong topical signals.
    SEO principle: headings tell search engines (and us) what a page is about.
    """
    return {
        f"h{level}": [
            tag.get_text(strip=True)
            for tag in soup.find_all(f"h{level}")
            if tag.get_text(strip=True)
        ]
        for level in (1, 2, 3)
    }


def _extract_body_text(soup: BeautifulSoup, max_chars: int = BODY_SNIPPET_MAX_CHARS) -> str:
    """
    Extract visible body text, stripping scripts, styles, and nav boilerplate.
    We cap at max_chars to keep downstream classifier calls cheap and fast.
    """
    # Remove noise tags — their text is never useful for topic classification
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
        tag.decompose()

    raw = soup.get_text(separator=" ", strip=True)

    # Collapse multiple whitespace/newlines into a single space
    cleaned = re.sub(r"\s+", " ", raw).strip()

    return cleaned[:max_chars]


def _extract_canonical(soup: BeautifulSoup) -> Optional[str]:
    """
    Pull the canonical URL if present — useful for deduplication at scale.
    At billions of URLs, many pages share content; canonical signals the authoritative one.
    """
    tag = soup.find("link", rel="canonical")
    return tag["href"].strip() if tag and tag.get("href") else None


# ── Public API ────────────────────────────────────────────────────────────────

def crawl(url: str) -> dict:
    """
    Main entry point.  Given any URL, returns a structured metadata dict.

    Returns
    -------
    {
        "url":          str   — original URL requested
        "status_code":  int   — HTTP status returned by the page
        "canonical":    str | None
        "title":        str | None
        "description":  str | None
        "keywords":     list[str]
        "og_tags":      dict
        "headings":     { "h1": [...], "h2": [...], "h3": [...] }
        "body_snippet": str   — first ~1500 chars of visible text
        "error":        str | None  — populated only on failure
    }
    """
    base: dict = {
        "url": url,
        "status_code": None,
        "canonical": None,
        "title": None,
        "description": None,
        "keywords": [],
        "og_tags": {},
        "headings": {"h1": [], "h2": [], "h3": []},
        "body_snippet": "",
        "error": None,
    }

    try:
        fetch_url = _normalize_crawl_url(url)
        html, status = _fetch_html(fetch_url)
        base["status_code"] = status

        soup = BeautifulSoup(html, "html.parser")

        base["canonical"]    = _extract_canonical(soup)
        base["title"]        = soup.title.string.strip() if soup.title else None
        base["description"]  = _get_meta(soup, "description") or _get_meta(soup, "og:description")
        raw_keywords         = _get_meta(soup, "keywords") or ""
        base["keywords"]     = [k.strip() for k in raw_keywords.split(",") if k.strip()]
        base["og_tags"]      = _extract_og_tags(soup)
        base["headings"]     = _extract_headings(soup)
        base["body_snippet"] = _extract_body_text(soup)

    except requests.exceptions.Timeout:
        c, r = _connect_timeout_sec(), _request_timeout_sec()
        base["error"] = (
            f"Request timed out (connect≤{c:.0f}s, read≤{r:.0f}s per attempt, 2 tries). "
            "Common causes: bot/WAF stalling non-browser clients, or very slow hosts. "
            "HTTP was upgraded to HTTPS unless CRAWL_NO_HTTPS_UPGRADE=1. "
            "If this persists, raise CRAWL_TIMEOUT_SEC or use a headless browser (Playwright) for strict sites."
        )
    except requests.exceptions.HTTPError as e:
        base["error"] = f"HTTP error: {e.response.status_code}"
    except requests.exceptions.RequestException as e:
        base["error"] = f"Network error: {str(e)}"
    except Exception as e:
        base["error"] = f"Unexpected error: {str(e)}"

    return base
