"""
main.py
-------
FastAPI application — exposes the crawler + classifier as a REST API.

Endpoints:
  GET  /            → health check (used by GCP Cloud Run to verify the container is alive)
  POST /crawl       → main endpoint: crawl a URL, return metadata + topics
  GET  /crawl       → convenience GET variant (for quick browser / curl testing)

Design decisions:
  - FastAPI chosen over Flask: automatic OpenAPI docs at /docs, native async support,
    and Pydantic validation — important for a production-grade service.
  - Request/response models defined explicitly with Pydantic so the API contract is
    self-documenting and validated at the edge.
  - CORS enabled: necessary when a frontend calls this API from a browser.
  - Structured logging: at scale (billions of crawls), logs are your debugging lifeline.
"""

import logging
import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl

from crawler import crawl
from classifier import classify

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ── App bootstrap ─────────────────────────────────────────────────────────────
app = FastAPI(
    title="BrightEdge Web Crawler API",
    description=(
        "Fetches a URL with browser-like HTTP headers, extracts HTML metadata (title, description, "
        "OG tags, headings, body snippet), then assigns topics using Claude (if set), else Gemini "
        "(if set), else a keyword taxonomy. `classification_source` is `claude`, `gemini`, `keywords`, "
        "or `none`. Crawl may fail with timeouts or blocks on strict sites; see response `error`."
    ),
    version="1.0.0",
)

# Allow all origins during development / demo — tighten in production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request / Response models ─────────────────────────────────────────────────

class CrawlRequest(BaseModel):
    """POST body schema."""
    url: HttpUrl  # Pydantic validates this is a real URL before we even touch it

    class Config:
        json_schema_extra = {
            "example": {
                "url": "https://www.amazon.com/Cuisinart-CPT-122-Compact-2-Slice-Toaster/dp/B009GQ034C/"
            }
        }


class CrawlResponse(BaseModel):
    """Unified response schema — same shape for both POST and GET variants."""
    url:                    str
    status_code:            Optional[int]   = None
    canonical:              Optional[str]   = None
    title:                  Optional[str]   = None
    description:            Optional[str]   = None
    keywords:               list[str]       = []
    og_tags:                dict            = {}
    headings:               dict            = {}
    body_snippet:           str             = ""
    topics:                 list[str]       = []
    classification_source:  str             = "none"
    error:                  Optional[str]   = None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", tags=["Health"])
def health_check():
    """
    GCP Cloud Run calls this every 30s to verify the container is live.
    Must return 200 quickly — no heavy work here.
    """
    return {"status": "ok", "service": "brightedge-crawler"}


@app.post("/crawl", response_model=CrawlResponse, tags=["Crawler"])
def crawl_url_post(request: CrawlRequest):
    """
    **Primary endpoint.**

    Accepts a URL, fetches HTML, extracts metadata, and classifies topics.

    **Crawl:** Uses `requests` with Chrome-like headers, optional `http→https` upgrade, session +
    redirects, connect/read timeouts (`CRAWL_CONNECT_TIMEOUT_SEC`, `CRAWL_TIMEOUT_SEC`), and one
    retry on timeout. If `error` is set, `topics` are empty and classification is skipped.

    **Topics (in order):**

    1. **Claude** — if `ANTHROPIC_API_KEY` is set and the call succeeds (`classification_source`: `claude`).
    2. **Gemini** — if no topics yet and `GEMINI_API_KEY` is set; model from `GEMINI_MODEL`
       (default `gemini-2.5-flash`), with retries on 429/503 (`classification_source`: `gemini`).
    3. **Keywords** — curated taxonomy if both AI paths fail or no keys are set (`classification_source`: `keywords`).
    4. **None** — no usable title/description/body (`classification_source`: `none`).

    Response shape is always the same; check `error` and `classification_source` for what ran.
    """
    url = str(request.url)
    logger.info("Crawl request received: %s", url)

    metadata = crawl(url)

    # Only classify if the crawl itself didn't error out
    if not metadata.get("error"):
        metadata = classify(metadata)
    else:
        metadata["topics"] = []
        metadata["classification_source"] = "none"
        logger.warning("Skipping classification due to crawl error: %s", metadata["error"])

    return CrawlResponse(**metadata)


@app.get("/crawl", response_model=CrawlResponse, tags=["Crawler"])
def crawl_url_get(url: str = Query(..., description="Full URL to crawl", example="https://blog.rei.com/camp/how-to-introduce-your-indoorsy-friend-to-the-outdoors/")):
    """
    Same behavior as **POST /crawl** (crawl + Claude / Gemini / keyword classification).

    Quick test: `curl "https://<host>/crawl?url=https://example.com"`
    """
    logger.info("Crawl GET request: %s", url)

    # Basic guard — Pydantic HttpUrl isn't used here so we do a manual check
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")

    metadata = crawl(url)

    if not metadata.get("error"):
        metadata = classify(metadata)
    else:
        metadata["topics"] = []
        metadata["classification_source"] = "none"

    return CrawlResponse(**metadata)


# ── Local dev runner ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    # Cloud Run sets PORT env var; default to 8080 locally
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
