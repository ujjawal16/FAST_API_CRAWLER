"""
classifier.py
-------------
Classifies a crawled page into a ranked list of relevant topics.

Strategy (two-layer):
  Layer 1 — Claude API (primary)
      Sends title + description + body snippet to Claude.
      Returns 3-7 specific, hierarchical topics (e.g. "Kitchen Appliances > Toasters").
      Why Claude: it understands context, synonyms, and page intent — far better than
      pure keyword matching for SEO topic relevance.

  Layer 2 — Keyword heuristics (fallback)
      Runs if Claude is unavailable or the API key is not set.
      Scans title + description + body for keywords from a curated taxonomy.
      Deterministic, zero-cost, but shallower.

Design principle: Never let the classifier crash the crawler.
  All exceptions are caught and the fallback is invoked silently.
"""

import os
import re
import json
import time
import logging
import random
import requests
import anthropic
from typing import Optional

logger = logging.getLogger(__name__)

# ── Keyword taxonomy for fallback ─────────────────────────────────────────────
# Each key is a top-level topic; values are trigger keywords.
# Easily extensible — in production this would live in a DB or config file.

KEYWORD_TAXONOMY: dict[str, list[str]] = {
    "Kitchen & Appliances":     ["toaster", "blender", "microwave", "oven", "cuisinart", "kitchen appliance", "coffee maker", "air fryer"],
    "Electronics":              ["laptop", "smartphone", "tablet", "camera", "headphones", "television", "monitor", "charger"],
    "Outdoor & Camping":        ["hiking", "camping", "trail", "backpacking", "tent", "sleeping bag", "campfire", "trekking"],
    "Sports & Fitness":         ["workout", "fitness", "gym", "running shoes", "yoga mat", "exercise", "weightlifting", "treadmill"],
    "Fashion & Apparel":        ["clothing", "shoes", "dress", "shirt", "jacket", "fashion", "apparel", "jeans", "sneakers"],
    "Travel":                   ["hotel", "flight", "destination", "vacation", "tourism", "itinerary", "passport", "resort"],
    "Food & Recipes":           ["recipe", "cooking", "restaurant", "meal", "cuisine", "ingredient", "baking", "nutrition facts"],
    "Technology & Software":    [
        "software", "programming", "api", "cloud computing", "machine learning",
        "artificial intelligence", "generative ai", "tech industry", "big tech",
        "saas", "developer", "framework",
    ],
    "Health & Wellness":        ["health", "wellness", "medical", "doctor", "mental health", "supplement", "therapy", "symptoms"],
    "News & Media":             ["breaking news", "politics", "journalism", "world news", "press release", "media", "reporter", "cnn", "study finds"],
    "E-Commerce & Shopping":    ["add to cart", "buy now", "price", "product review", "free shipping", "discount", "amazon", "walmart"],
    "Finance":                  ["stock market", "investment", "cryptocurrency", "banking", "mutual fund", "insurance", "mortgage"],
    "Home & Garden":            ["furniture", "home decor", "gardening", "interior design", "living room", "bedroom", "landscaping"],
    "Automotive":               ["car model", "vehicle review", "engine specs", "test drive", "automobile", "dealership", "mpg", "horsepower"],
}


# ── Claude-powered classification ─────────────────────────────────────────────

def _classify_with_claude(title: str, description: str, body: str) -> list[str]:
    """
    Uses Claude claude-sonnet-4-20250514 to generate high-quality, hierarchical topics.

    Prompt engineering choices:
    - We give title, description AND body snippet for maximum context.
    - We ask for 3–7 topics in order of relevance (most relevant first).
    - We request JSON output so we can parse it reliably.
    - We set max_tokens=300 — topics are short, this keeps cost low.
    """
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    prompt = f"""You are an SEO content classifier. Analyze the webpage content below and return a JSON array of 3 to 7 relevant topics, ordered from most to least relevant.

Rules:
- Be specific (prefer "Kitchen Appliances > Toasters" over just "Products")
- Use hierarchical format where appropriate (Parent > Child)
- Focus on what the page is primarily ABOUT, not just mentions
- Return ONLY a valid JSON array of strings, no other text

Webpage content:
Title: {title or 'N/A'}
Description: {description or 'N/A'}
Body: {body[:1000] or 'N/A'}

JSON array of topics:"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()

    # Robustly parse — strip markdown fences if model adds them
    raw = re.sub(r"```json|```", "", raw).strip()
    topics = json.loads(raw)

    if isinstance(topics, list):
        return [str(t) for t in topics if t]
    return []


# ── Keyword fallback classification ───────────────────────────────────────────

def _classify_with_keywords(title: str, description: str, body: str) -> list[str]:
    """
    Lightweight keyword matching against the KEYWORD_TAXONOMY.
    Scores each category by number of keyword hits in the combined text.
    Returns categories sorted by hit count (highest first).

    Tradeoffs vs Claude:
      + Always available, zero API cost, deterministic
      - Misses context and synonyms (e.g. "brew" won't match "Coffee")
      - Can't distinguish primary from incidental mentions
    """
    combined = f"{title} {description} {body}".lower()
    scores: dict[str, int] = {}

    for category, keywords in KEYWORD_TAXONOMY.items():
        hits = sum(1 for kw in keywords if kw.lower() in combined)
        if hits > 0:
            scores[category] = hits

    # Sort by score descending, return top 5
    ranked = sorted(scores, key=lambda c: scores[c], reverse=True)
    return ranked[:5]


# ── Gemini-powered classification (free tier) ─────────────────────────────────
# Default model: gemini-2.5-flash (stable). Avoid gemini-2.0-flash — deprecated per Google;
# it can return 404 as models are retired. Override: GEMINI_MODEL=gemini-2.5-flash-lite
GEMINI_MODEL_DEFAULT = "gemini-2.5-flash"


def _gemini_generate_url() -> str:
    model = (os.environ.get("GEMINI_MODEL") or GEMINI_MODEL_DEFAULT).strip()
    return (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
# Free tier is often ~15 RPM — faster retries all hit the same window and keep returning 429.
GEMINI_MAX_ATTEMPTS = int(os.environ.get("GEMINI_MAX_ATTEMPTS", "6"))
GEMINI_RETRYABLE_STATUS = frozenset({429, 503})
# Minimum first wait after 429 (seconds); ~4s+ between calls respects 15 RPM.
GEMINI_429_MIN_WAIT = float(os.environ.get("GEMINI_429_MIN_WAIT_SEC", "4.5"))
GEMINI_BACKOFF_CAP_SEC = float(os.environ.get("GEMINI_BACKOFF_CAP_SEC", "120"))


def _gemini_retry_delay_from_error_body(response: requests.Response) -> Optional[float]:
    """Parse google.rpc.RetryInfo retryDelay from JSON error body, if present."""
    try:
        data = response.json()
        for d in (data.get("error") or {}).get("details") or []:
            if not isinstance(d, dict):
                continue
            rd = d.get("retryDelay")
            if rd is None:
                continue
            if isinstance(rd, (int, float)):
                return float(rd)
            if isinstance(rd, str):
                rd = rd.strip().removesuffix("s").strip()
                try:
                    return float(rd)
                except ValueError:
                    continue
    except (ValueError, TypeError, json.JSONDecodeError):
        pass
    return None


def _gemini_backoff_seconds(
    attempt: int,
    response: Optional[requests.Response],
    status_code: int,
) -> float:
    if response is not None:
        ra = response.headers.get("Retry-After")
        if ra:
            try:
                return min(float(ra), GEMINI_BACKOFF_CAP_SEC)
            except ValueError:
                pass
        if status_code == 429:
            parsed = _gemini_retry_delay_from_error_body(response)
            if parsed is not None:
                return min(max(parsed, 0.5), GEMINI_BACKOFF_CAP_SEC)

    if status_code == 429:
        # Exponential from a floor so early retries are not all <1s apart.
        base = GEMINI_429_MIN_WAIT * (2**attempt)
        return min(base + random.uniform(0, 0.75), GEMINI_BACKOFF_CAP_SEC)

    return min(2**attempt * 0.75, 30.0) + random.uniform(0, 0.25)


def _classify_with_gemini(title: str, description: str, body: str) -> list[str]:
    """
    Google AI Gemini via REST generateContent (same contract as ai.google.dev examples).

    Env:
      GEMINI_API_KEY — required
      GEMINI_MODEL — optional, default gemini-2.5-flash (e.g. gemini-2.5-flash-lite)

    Free tier limits (typical): low RPM / RPD — expect 429 if you burst requests.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set")

    prompt = f"""You are an SEO content classifier. Return a JSON array of 3 to 7 relevant topics for this webpage, ordered most to least relevant. Be specific (e.g. "Kitchen Appliances > Toasters"). Return ONLY valid JSON, no other text.

Title: {title or 'N/A'}
Description: {description or 'N/A'}
Body: {body[:1000] or 'N/A'}"""

    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    resp: Optional[requests.Response] = None
    for attempt in range(GEMINI_MAX_ATTEMPTS):
        resp = requests.post(
            _gemini_generate_url(),
            headers=headers,
            json=payload,
            timeout=15,
        )
        if resp.ok:
            break
        if resp.status_code in GEMINI_RETRYABLE_STATUS and attempt < GEMINI_MAX_ATTEMPTS - 1:
            wait = _gemini_backoff_seconds(attempt, resp, resp.status_code)
            logger.info(
                "Gemini %s, retrying in %.1fs (%d/%d)",
                resp.reason,
                wait,
                attempt + 1,
                GEMINI_MAX_ATTEMPTS,
            )
            time.sleep(wait)
            continue
        # Do not include the request URL in the message — it would leak the API key into logs.
        raise requests.HTTPError(
            f"{resp.status_code} {resp.reason} from Gemini generateContent",
            response=resp,
        )

    assert resp is not None and resp.ok
    raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    raw = re.sub(r"```json|```", "", raw).strip()
    topics = json.loads(raw)
    return [str(t) for t in topics if t] if isinstance(topics, list) else []


# ── Public API ─────────────────────────────────────────────────────────────────

def classify(metadata: dict) -> dict:
    """
    Given a metadata dict (from crawler.crawl()), returns an enriched dict
    with a 'topics' key and 'classification_source' indicating which layer was used.

    classification_source values:
      'claude'   — Claude API returned results
      'gemini'   — Google Gemini generateContent returned results
      'keywords' — fallback keyword matching was used
      'none'     — no extractable content or classification failed entirely
    """
    title       = metadata.get("title") or ""
    description = metadata.get("description") or ""
    body        = metadata.get("body_snippet") or ""

    # If we have nothing to work with, skip classification entirely
    if not any([title, description, body]):
        metadata["topics"] = []
        metadata["classification_source"] = "none"
        return metadata

    topics = []
    source = "keywords"  # assume fallback unless a higher layer succeeds

    # ── Layer 1: Try Claude (best quality, paid) ──────────────────────────────
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            topics = _classify_with_claude(title, description, body)
            source = "claude"
            logger.info("Classification via Claude: %s", topics)
        except Exception as e:
            logger.warning("Claude classification failed (%s), trying Gemini.", e)

    # ── Layer 2: Try Gemini (good quality, FREE) ──────────────────────────────
    if not topics and os.environ.get("GEMINI_API_KEY"):
        try:
            topics = _classify_with_gemini(title, description, body)
            source = "gemini"
            logger.info("Classification via Gemini: %s", topics)
        except Exception as e:
            logger.warning("Gemini classification failed (%s), using keywords.", e)

    # ── Layer 3: Keyword fallback (always free, no key needed) ────────────────
    if not topics:
        try:
            topics = _classify_with_keywords(title, description, body)
            source = "keywords"
            logger.info("Classification via keywords: %s", topics)
        except Exception as e:
            logger.error("Keyword classification also failed: %s", e)
            topics = []
            source = "none"

    metadata["topics"] = topics
    metadata["classification_source"] = source
    return metadata