# AI Web Crawler 

> **AI Tools Used:**
> - **Claude API** (Anthropic `claude-sonnet-4-20250514`) — primary page topic classification
> - **Google Gemini API** (`gemini-2.5-flash`) — free-tier fallback classifier
> - **Claude claude-sonnet-4-20250514** (chat) — design consultation, code review, prompt engineering during development

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Part 1 — Core Crawler: Setup & API Usage](#2-part-1--core-crawler)
3. [Part 2 — Scale Architecture Design](#3-part-2--scale-architecture-design)
4. [Part 3 — PoC Roadmap & Release Plan](#4-part-3--poc-roadmap--release-plan)
5. [Repository Structure](#5-repository-structure)

---

## 1. Project Overview

This service crawls any given URL, extracts structured HTML metadata, and classifies the page into relevant SEO topics using a three-layer strategy that degrades gracefully:

```
Claude API  →  Google Gemini API  →  Keyword taxonomy fallback
 (best)           (free tier)          (always available)
```

The system is designed for two modes of operation:
- **Single-URL API** (Part 1): Real-time crawl + classify via REST endpoint
- **Bulk pipeline** (Part 2): Ingest billions of URLs from flat file or MySQL, process at scale, store in BigQuery

**Tech stack:** Python 3.9+ · FastAPI · BeautifulSoup4 · Anthropic SDK · Google Generative AI · Docker · AWS EC2 / GCP Cloud Run

---

## 2. Part 1 — Core Crawler

### 2.1 Local Setup

```bash
# Clone the repo
git clone https://github.com/<your-username>/<your-repo>.git
cd <your-repo>

# Create virtual environment
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Optional: set AI classifier keys (keyword fallback works without either)
export ANTHROPIC_API_KEY="sk-ant-..."   # Claude (paid, best quality)
export GEMINI_API_KEY="AIza..."         # Gemini (free, 1000 req/day)

# Run the server
python main.py
# → http://localhost:8080
# → Interactive API docs: http://localhost:8080/docs

# Run tests
pip install pytest && pytest test_crawler.py -v
```

### 2.2 URL Normalization (Edge Cases Handled)

The crawler handles all URL formats from the assignment spec:

| Input format | Handled? | How |
|---|---|---|
| `https://www.amazon.com/...` | ✅ | Direct fetch |
| `http://blog.rei.com/...` | ✅ | Auto-upgraded to `https://` |
| `amazon.com` (bare domain) | ✅ | `https://` prepended automatically |
| `www.walmart.com` | ✅ | `https://` prepended automatically |

URL normalization happens in `crawler.py` before any network call:
```python
# Bare domains and http:// are normalized before fetching
amazon.com  →  https://www.amazon.com
http://blog.rei.com/...  →  https://blog.rei.com/...
```

### 2.3 API Reference

#### `POST /crawl` — Primary endpoint

```bash
curl -X POST http://localhost:8080/crawl \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.amazon.com/Cuisinart-CPT-122-Compact-2-Slice-Toaster/dp/B009GQ034C/"}'
```

#### `GET /crawl` — Quick browser test

```bash
curl "http://localhost:8080/crawl?url=https://blog.rei.com/camp/how-to-introduce-your-indoorsy-friend-to-the-outdoors/"
```

#### `GET /` — Health check

```bash
curl http://localhost:8080/
# → {"status": "ok", "service": "brightedge-crawler"}
```

### 2.4 Example Responses

**Amazon product page (toaster):**
```json
{
  "url": "https://www.amazon.com/Cuisinart-CPT-122-Compact-2-Slice-Toaster/dp/B009GQ034C/",
  "status_code": 200,
  "canonical": "https://www.amazon.com/dp/B009GQ034C",
  "title": "Cuisinart CPT-122 Compact 2-Slice Toaster, Brushed Chrome",
  "description": "Buy Cuisinart CPT-122 Compact 2-Slice Toaster with wide slots...",
  "keywords": ["toaster", "cuisinart", "kitchen appliances"],
  "og_tags": { "og:type": "product", "og:title": "Cuisinart CPT-122 Toaster" },
  "headings": {
    "h1": ["Cuisinart CPT-122 Compact 2-Slice Toaster"],
    "h2": ["Product details", "Customer reviews"],
    "h3": []
  },
  "body_snippet": "Cuisinart CPT-122 Compact 2-Slice Toaster. Wide slots accommodate thick breads...",
  "topics": ["Kitchen Appliances > Toasters", "E-Commerce > Amazon Product", "Small Kitchen Appliances"],
  "classification_source": "gemini",
  "error": null
}
```

**REI blog post:**
```json
{
  "url": "https://blog.rei.com/camp/how-to-introduce-your-indoorsy-friend-to-the-outdoors/",
  "title": "How to Introduce Your Indoorsy Friend to the Outdoors",
  "topics": ["Outdoor & Camping > Beginner Guides", "Hiking & Trails", "Outdoor Lifestyle"],
  "classification_source": "gemini"
}
```

**CNN tech article:**
```json
{
  "url": "https://www.cnn.com/2025/09/23/tech/google-study-90-percent-tech-jobs-ai",
  "title": "Google study finds AI could impact 90% of tech jobs",
  "topics": ["Technology & AI > Labor Impact", "News & Media > Tech Industry", "Jobs & Careers"],
  "classification_source": "gemini"
}
```

### 2.5 JavaScript-Heavy Pages (Amazon, Walmart, BestBuy)

Major retail sites render significant content client-side via JavaScript. Our `requests` + `BeautifulSoup` approach captures all static HTML (title, meta tags, OG tags, structured data) which is sufficient for metadata classification. For pages where body content is entirely JS-rendered, we fall back to OG tags and title for classification — which are always present in the static HTML.

For full JS rendering (future enhancement), Playwright can be toggled on per-domain:
```python
# Future: headless browser path for JS-heavy domains
HEADLESS_DOMAINS = {"amazon.com", "walmart.com", "bestbuy.com"}
```

### 2.6 Cloud Deployment (AWS EC2 — Live)

The service is deployed and running on AWS EC2:

```bash
# SSH into EC2 instance
ssh -i your-key.pem ec2-user@<your-ec2-ip>

# Run with screen so it persists after logout
screen -S crawler
export GEMINI_API_KEY="AIza..."
python main.py

# Test the live endpoint
curl "http://<ec2-ip>:8080/crawl?url=https://www.amazon.com"
```

**GCP Cloud Run (alternative):**
```bash
gcloud builds submit --config cloudbuild.yaml \
  --substitutions _GEMINI_API_KEY="AIza..."
# → Service URL: https://brightedge-crawler-xxxx-uc.a.run.app
```

---

## 3. Part 2 — Scale Architecture Design

### 3.1 The Challenge at Billions of URLs

| Challenge | Impact | Scale |
|-----------|--------|-------|
| Network I/O: 100–500ms per page | Parallelism required | 1B URLs = ~55,000 req/min needed |
| Pages fail transiently (429, 503, timeout) | Retries with backoff | ~5–10% of URLs fail first attempt |
| Duplicate URLs across monthly batches | Waste without dedup | Easily 20–30% overlap across months |
| LLM API cost per URL | Must batch + cache | $0.001/page × 1B = $1M without optimization |
| Storage of billions of metadata records | Needs columnar + partitioned DB | ~2KB/record × 1B = 2TB/month |
| "Millions of requests on the content" | Separate read API needed | Query layer distinct from crawl pipeline |

### 3.2 High-Level Architecture

```
╔══════════════════════════════════════════════════════════════════════╗
║                        INGESTION LAYER                               ║
║                                                                      ║
║  ┌─────────────────────┐      ┌──────────────────────────────────┐  ║
║  │  Text File Input    │      │  MySQL Input (url_queue table)   │  ║
║  │  (billions of URLs) │      │  SELECT url FROM url_queue       │  ║
║  │  Split into chunks  │      │  WHERE year_month = '2025-07'    │  ║
║  │  of 10,000 URLs     │      │  AND status = 'pending'          │  ║
║  └──────────┬──────────┘      └──────────────┬───────────────────┘  ║
║             └──────────────┬─────────────────┘                      ║
║                            ▼                                         ║
║             [URL Ingestion Service]                                  ║
║       • Normalize URLs (bare domains, http→https)                   ║
║       • Deduplicate via Redis Bloom Filter (O(1), ~1.2GB for 1B)    ║
║       • Publish valid URLs to Pub/Sub in batches of 1,000           ║
║       • Update MySQL status: 'pending' → 'queued'                   ║
╚══════════════════════════════════╦═══════════════════════════════════╝
                                   ║
                                   ▼
╔══════════════════════════════════════════════════════════════════════╗
║                         QUEUE LAYER                                  ║
║                                                                      ║
║            Google Cloud Pub/Sub — "urls-to-crawl"                   ║
║       • Durable, at-least-once delivery, 7-day retention            ║
║       • Messages ordered by domain hash (rate limit enforcement)    ║
║       • Dead Letter Topic for URLs that fail 5+ times               ║
╚══════════════════════════╦═══════════════════════════════════════════╝
                           ║  (parallel pull, up to 100 workers)
         ┌─────────────────┼─────────────────┐
         ▼                 ▼                 ▼
╔══════════════╗  ╔══════════════╗  ╔══════════════╗
║  Crawler     ║  ║  Crawler     ║  ║  Crawler     ║  ← Cloud Run
║  Worker #1   ║  ║  Worker #2   ║  ║  Worker #N   ║    autoscaled
║              ║  ║              ║  ║              ║    0–100 instances
║ 1. Fetch HTML║  ║ 1. Fetch HTML║  ║ 1. Fetch HTML║
║ 2. Parse meta║  ║ 2. Parse meta║  ║ 2. Parse meta║
║ 3. Classify  ║  ║ 3. Classify  ║  ║ 3. Classify  ║
║ 4. Write BQ  ║  ║ 4. Write BQ  ║  ║ 4. Write BQ  ║
║ 5. Ack msg   ║  ║ 5. Ack msg   ║  ║ 5. Ack msg   ║
╚══════╦═══════╝  ╚══════╦═══════╝  ╚══════╦═══════╝
       └─────────────────┼─────────────────┘
                         ▼
╔══════════════════════════════════════════════════════════════════════╗
║                       STORAGE LAYER                                  ║
║                                                                      ║
║  BigQuery (crawl_metadata)    GCS (raw HTML archive)                ║
║  ← analytical queries          ← gzip compressed, cheap long-term  ║
║  ← partitioned by year_month   ← pointer stored in BigQuery         ║
║                                                                      ║
║  Redis                                                               ║
║  ← Bloom filter (dedup)                                             ║
║  ← Domain rate limit counters                                       ║
║  ← Classification result cache (title hash → topics)               ║
╚══════════════════════════╦═══════════════════════════════════════════╝
                           ║
                           ▼
╔══════════════════════════════════════════════════════════════════════╗
║                   CONTENT QUERY API LAYER                            ║
║              (serves "millions of requests on the content")          ║
║                                                                      ║
║   FastAPI service — read-only queries on crawled metadata           ║
║   GET /metadata?url=...      → fetch metadata for a specific URL    ║
║   GET /topics?domain=...     → all topics seen for a domain         ║
║   GET /search?topic=...      → URLs classified under a topic        ║
║   Backed by: BigQuery (analytics) + Redis cache (hot paths)         ║
╚══════════════════════════╦═══════════════════════════════════════════╝
                           ║
                           ▼
╔══════════════════════════════════════════════════════════════════════╗
║                    OBSERVABILITY LAYER                               ║
║                                                                      ║
║  Cloud Monitoring · Cloud Logging · Grafana · PagerDuty · Slack     ║
╚══════════════════════════════════════════════════════════════════════╝
```

### 3.3 Unified Data Schema

The schema is defined once and flows through all three layers: **Input → Processing → Output.**

#### Input Schema — MySQL `url_queue` table

```sql
CREATE TABLE url_queue (
  id            BIGINT       AUTO_INCREMENT PRIMARY KEY,
  url           TEXT         NOT NULL,
  year_month    CHAR(7)      NOT NULL,        -- e.g. '2025-07' — matches assignment spec
  domain        VARCHAR(255) NOT NULL,        -- extracted at insert time
  status        ENUM('pending','queued','done','failed') DEFAULT 'pending',
  retry_count   TINYINT      DEFAULT 0,
  created_at    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
  updated_at    TIMESTAMP    ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_status_month (status, year_month),
  INDEX idx_domain (domain)
);

-- Query pattern: pull pending URLs for a given month in batches
SELECT url FROM url_queue
WHERE year_month = '2025-07'
  AND status = 'pending'
ORDER BY id
LIMIT 10000;
```

#### Processing Schema — Pub/Sub message envelope

```json
{
  "url": "https://www.amazon.com/dp/B009GQ034C",
  "year_month": "2025-07",
  "domain": "amazon.com",
  "enqueued_at": "2025-07-01T00:00:00Z",
  "attempt": 1
}
```

#### Output Schema — BigQuery `crawl_metadata` table

```sql
CREATE TABLE crawl_metadata (
  -- Identity
  url                   STRING    NOT NULL,
  domain                STRING    NOT NULL,       -- clustered for domain queries
  year_month            STRING    NOT NULL,       -- '2025-07' — matches MySQL input
  crawl_date            DATE      NOT NULL,       -- partition key
  crawl_timestamp       TIMESTAMP NOT NULL,

  -- HTTP
  status_code           INT64,
  canonical             STRING,
  crawl_duration_ms     INT64,

  -- Metadata (mirrors crawler.py output exactly)
  title                 STRING,
  description           STRING,
  keywords              ARRAY<STRING>,
  og_tags               JSON,                    -- full OG tag map
  h1_first              STRING,
  h2_first              STRING,
  body_snippet          STRING,

  -- Classification (mirrors classifier.py output exactly)
  topics                ARRAY<STRING>,
  classification_source STRING,                  -- 'claude'|'gemini'|'keywords'|'none'

  -- Storage pointer
  raw_html_gcs_path     STRING,                  -- gs://bucket/2025-07/domain/sha256.html.gz

  -- Pipeline health
  error                 STRING,                  -- null on success
  worker_id             STRING                   -- which Cloud Run instance processed this
)
PARTITION BY crawl_date
CLUSTER BY domain, year_month;
```

**Why unified:** every field in BigQuery maps directly to a key in the `crawl()` and `classify()` output dicts from Part 1. No transformation layer needed — the crawler writes its output dict directly as a BigQuery row.

### 3.4 Content Query API (Serving Millions of Requests)

The assignment requires the system to "allow millions of requests on the content." This is a separate read API layer on top of the stored metadata:

```
GET /metadata?url=https://amazon.com/dp/B009GQ034C
→ Returns stored crawl metadata for that URL

GET /topics?domain=amazon.com&year_month=2025-07
→ Returns all unique topics seen across that domain for July

GET /search?topic=Kitchen+Appliances&year_month=2025-07&limit=100
→ Returns URLs classified under that topic

GET /stats?year_month=2025-07
→ Returns crawl summary: total URLs, success rate, top topics, top domains
```

**Handling millions of read requests:**

| Layer | Role |
|-------|------|
| Redis (L1 cache) | Cache hot URL lookups — 95% of reads served from memory, <1ms |
| BigQuery (L2) | Cache miss falls through to BQ — 100ms–2s, handles arbitrary SQL |
| CDN (Cloudflare/GCP CDN) | Cache `/stats` and `/topics` responses at edge — zero BQ cost for repeated queries |

### 3.5 Deduplication Strategy

```
Before publishing any URL to Pub/Sub:

  Step 1: Normalize URL
    amazon.com → https://www.amazon.com/
    http://example.com → https://example.com/

  Step 2: Bloom Filter check (Redis)
    KEY = sha256(normalized_url)
    EXISTS crawled:{KEY} for year_month=2025-07
    → YES: skip, mark MySQL row as 'done' (already crawled this month)
    → NO:  publish to Pub/Sub, SET crawled:{KEY} EX 2592000 (30-day TTL)

Bloom Filter sizing (Redis):
  1B URLs, 1% false positive rate → ~1.2GB RAM
  False positive = we skip a URL we haven't actually crawled.
  Acceptable: 1% miss rate is far cheaper than 20–30% duplicate crawls.
```

### 3.6 Domain Rate Limiting

Responsible crawling: we must not overwhelm any single domain.

```
Per-domain rate limit (Redis counter):
  INCR rate:{domain}:{minute_bucket}
  EXPIRE rate:{domain}:{minute_bucket} 60

  Default: max 60 req/min per domain (1 req/sec)
  Override table in BigQuery: domain_config (domain, max_rps, crawl_allowed)

On 429 / 503 from target site:
  Exponential backoff: wait = min(2^attempt × 0.5s, 60s)
  After 5 failures: move URL to Dead Letter Topic for manual review
```

### 3.7 Recrawl Strategy (Monthly Cycles)

The assignment specifies "billions of URLs for [month]" — implying monthly batch cycles.

```
Monthly recrawl policy:
  - All URLs recrawled every 30 days (full refresh)
  - High-priority domains (amazon.com, walmart.com): recrawled weekly
  - Canonical dedup: if canonical URL matches an existing row in BQ this month, skip

Implemented via:
  - Redis TTL of 30 days on Bloom filter keys
  - MySQL year_month field allows querying by month independently
  - BigQuery partitioned by crawl_date — each month is a separate partition
```

### 3.8 Cost Optimization

| Lever | Strategy | Estimated Saving |
|-------|----------|-----------------|
| LLM classification | Batch 20 URLs per Claude call; cache by title hash in Redis | ~80% reduction |
| Gemini free tier | Use Gemini (free) before Claude (paid) for classification | ~$600/month saved |
| Storage | Compress raw HTML in GCS with gzip (~70% smaller) | ~$70/month saved |
| BigQuery queries | Partition pruning + clustering — never full table scans | ~90% query cost reduction |
| Dedup | Bloom filter prevents re-crawling 20–30% duplicates | ~$200/month saved |
| Retries | Dead-letter after 5 failures — stop spending on broken URLs | ~$50/month saved |

**Cost estimate at 1B URLs/month:**

| Component | Cost |
|-----------|------|
| Cloud Run (crawling compute) | ~$200–400 |
| Gemini API (free up to 1K/day, then $0.0001/page) | ~$50–100 |
| Claude API (overflow, batched) | ~$100–200 |
| BigQuery storage (10TB) | ~$20 |
| GCS raw HTML (compressed) | ~$100 |
| Redis (Cloud Memorystore) | ~$50 |
| Pub/Sub | ~$40 |
| **Total** | **~$560–910/month** |

### 3.9 SLOs and SLAs

| Metric | SLO Target | Measurement |
|--------|-----------|-------------|
| Crawl success rate | ≥ 95% of submitted URLs return metadata | Per 24-hour batch |
| P95 crawl latency | ≤ 3s per URL | Rolling 1-hour window |
| Classification accuracy | ≥ 90% topic relevance | Weekly human audit of 1,000 samples |
| Query API availability | 99.9% uptime | Monthly |
| Data freshness | Metadata in BigQuery ≤ 4 hours after crawl | Per batch |
| Queue backlog | Cleared within 24 hours of ingestion | Per daily batch |
| P95 query latency | ≤ 200ms (Redis hit) / ≤ 2s (BigQuery fallback) | Rolling 5-min window |

**SLA (contractual, customer-facing):**
- Query API: 99.5% uptime, P95 < 500ms
- Crawl pipeline: 95% of URLs processed within 48 hours
- Breach: Service credits per standard GCP SLA model

### 3.10 Key Monitoring Metrics and Tools

**Tools:** Cloud Monitoring + Cloud Logging (GCP-native) · Grafana (dashboards) · PagerDuty (on-call) · Slack (warnings)

**Crawl pipeline metrics:**

| Metric | Type | Alert threshold |
|--------|------|----------------|
| `crawler/success_rate` | Gauge | CRITICAL < 80% for 15min |
| `crawler/latency_p95` | Histogram | WARNING > 5s for 5min |
| `pubsub/backlog_depth` | Counter | WARNING > 5M messages |
| `classifier/source_ratio` | Gauge | WARNING if `keywords` > 50% (AI degraded) |
| `crawler/error_by_type` | Counter | Track 403/429/timeout separately |

**Business metrics (BigQuery dashboard, refreshed hourly):**

```sql
-- URLs crawled per hour by topic
SELECT DATE_TRUNC(crawl_timestamp, HOUR) as hour,
       topic, COUNT(*) as url_count
FROM crawl_metadata, UNNEST(topics) as topic
WHERE crawl_date = CURRENT_DATE()
GROUP BY 1, 2 ORDER BY 1, 3 DESC;

-- Error rate breakdown
SELECT error, COUNT(*) as count,
       ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 2) as pct
FROM crawl_metadata
WHERE crawl_date = CURRENT_DATE() AND error IS NOT NULL
GROUP BY error ORDER BY 2 DESC;
```

---

## 4. Part 3 — PoC Roadmap & Release Plan

### 4.1 Engineering Task Breakdown

#### ✅ Milestone 1 — Working Crawler (Week 1 — COMPLETE)

| Task | Est. | Status |
|------|------|--------|
| Core crawler — `crawler.py` | 1d | ✅ Done |
| Three-layer classifier — `classifier.py` (Claude→Gemini→keywords) | 1d | ✅ Done |
| FastAPI REST service — `main.py` | 0.5d | ✅ Done |
| Docker container + EC2 deploy | 0.5d | ✅ Done |
| Unit tests (mocked HTTP) | 0.5d | ✅ Done |
| **Total** | **3.5d** | |

#### Milestone 2 — Scale Infrastructure (Weeks 2–3)

| Task | Est. | Owner | ETA |
|------|------|-------|-----|
| MySQL reader service (batch pull by year_month) | 2d | Backend | Day 8 |
| URL normalization + Bloom filter dedup (Redis) | 1d | Backend | Day 9 |
| Pub/Sub producer + domain rate limiting | 1d | Backend | Day 10 |
| Pub/Sub consumer worker (pull + crawl + write BQ) | 2d | Backend | Day 12 |
| BigQuery schema creation + streaming write | 1d | Data Eng | Day 13 |
| GCS raw HTML archiving (gzip) | 0.5d | Backend | Day 13 |
| Cloud Run worker autoscaling config | 0.5d | DevOps | Day 14 |
| **Total** | **8d** | | Week 3 end |

#### Milestone 3 — Query API + Observability (Week 4)

| Task | Est. | Owner | ETA |
|------|------|-------|-----|
| Content query API (`/metadata`, `/topics`, `/search`) | 2d | Backend | Day 16 |
| Redis caching layer for query API | 1d | Backend | Day 17 |
| Structured logging → Cloud Logging | 0.5d | Backend | Day 18 |
| Grafana dashboards (crawl + query metrics) | 1d | DevOps | Day 19 |
| Alerting policies (PagerDuty + Slack) | 0.5d | DevOps | Day 19 |
| Retry logic + Dead Letter Queue handling | 1d | Backend | Day 20 |
| **Total** | **6d** | | Week 4 end |

#### Milestone 4 — PoC Validation (Week 5)

| Task | Est. | Owner | ETA |
|------|------|-------|-----|
| End-to-end test: 1M URL batch from flat file | 2d | QA + Eng | Day 22 |
| End-to-end test: 1M URL batch from MySQL | 1d | QA + Eng | Day 23 |
| Query API load test: 10K concurrent requests | 1d | QA | Day 24 |
| Human classification quality audit (1K samples) | 1d | Product | Day 24 |
| Cost audit: actual vs projected | 0.5d | Eng | Day 25 |
| Documentation finalization | 0.5d | All | Day 25 |
| **Total** | **6d** | | Week 5 end |

**Total PoC timeline: 5 weeks, team of 3–4 engineers.**

### 4.2 Potential Blockers — Known, Trivial, and Unknown

#### Known & Solvable (with ETAs)

| Blocker | Type | Resolution | ETA |
|---------|------|-----------|-----|
| **Bot detection / 403s** on retail sites | Known | Rotate User-Agents, add jitter delays, residential proxies for priority domains | Week 2, Day 2 |
| **JavaScript-rendered pages** (Amazon, BestBuy SPAs) | Known | Add Playwright headless browser path, toggled per-domain | Week 3, Day 2 |
| **Gemini / Claude rate limits** | Known | Exponential backoff (built in), request batching, cache by title hash | Already built |
| **BigQuery streaming insert quotas** (10MB/s default) | Known | Switch to BigQuery Storage Write API for 10× throughput | Week 2, Day 5 |
| **Pub/Sub message ordering at scale** | Known | Use domain as ordering key; pre-shard topics by `sha256(domain) % N` | Week 2, Day 3 |
| **MySQL connection pool exhaustion** at high read rate | Known | Use SQLAlchemy pool with max_overflow=20; read replicas for scale | Week 2, Day 1 |
| **Bare domain URLs** (amazon.com, walmart.com) | Trivial | URL normalization (prepend https://) — already implemented | ✅ Done |
| **http:// URLs** timing out due to redirect | Trivial | Auto-upgrade to https:// before first request — already implemented | ✅ Done |
| **Encoding issues** (non-UTF-8 pages) | Trivial | `chardet` library for encoding detection, fallback to latin-1 | Week 2, Day 1 |

#### Unknown Risks

| Risk | Probability | Impact | Mitigation |
|------|------------|--------|-----------|
| Domain IP blocks at crawl volume | Medium | High | Per-domain crawl rate config table in BigQuery |
| Legal ToS violations on crawled domains | Medium | High | Legal review of target domain list before production |
| Gemini/Claude API pricing model changes | Low | Medium | Keyword fallback always operational; cost alerts configured |
| BigQuery query costs spike from unoptimized ad-hoc queries | Medium | Medium | Enforce partition filter requirement; alert on >$10/query |
| Pages that return 200 but deliver bot-detection HTML | High | Low | Detect by checking body length < 500 chars; re-queue |

### 4.3 PoC Evaluation Methodology

#### How to Evaluate the PoC (three dimensions)

**Dimension 1 — Functional Correctness**

```
Test suite: run against all assignment URLs
  ✓ https://amazon.com/Cuisinart-...          → metadata extracted, topics classified
  ✓ https://blog.rei.com/camp/...             → metadata extracted, topics classified
  ✓ https://www.cnn.com/2025/09/23/tech/...  → metadata extracted, topics classified
  ✓ amazon.com  (bare domain)                → normalized, crawled, classified
  ✓ walmart.com (bare domain)                → normalized, crawled, classified
  ✓ bestbuy.com (bare domain)                → normalized, crawled, classified

Classification quality (human audit):
  Sample 1,000 random URLs from 1M test batch.
  Two human reviewers independently rate: "Are the topics relevant?" (yes/no)
  Pass threshold: ≥ 90% agreement, ≥ 90% "yes" ratings
```

**Dimension 2 — Performance & Scale**

```
Load test protocol (locust.io):
  Phase 1: Ramp from 0 to 100 concurrent requests over 5 minutes
  Phase 2: Hold at 100 concurrent for 10 minutes
  Phase 3: Ramp to 1,000 concurrent over 5 minutes
  Phase 4: Hold at 1,000 concurrent for 10 minutes

  Pass criteria:
  ✓ P95 crawl latency ≤ 3s at 1,000 concurrent
  ✓ Error rate < 5%
  ✓ Query API P95 ≤ 200ms (Redis hit)

Bulk pipeline test:
  Input: 1M URLs from flat file
  Expected: processed within 24 hours
  Verify: 95% in BigQuery with non-null title or body
```

**Dimension 3 — Cost Validation**

```
Run 1M URL batch, measure actual GCP costs via billing export
Compare against projection:
  ✓ Actual within 20% of projected = PASS
  ✓ Actual > 20% over projected = investigate before full scale
```

#### Go / No-Go Decision Gate (End of Week 5)

30-minute review meeting. All criteria must be green to proceed:

| Criterion | Target | Measurement |
|-----------|--------|-------------|
| All 6 assignment URLs crawled successfully | 6/6 | Manual test |
| 1M flat-file batch processed | ≤ 24 hours | Pipeline run |
| 1M MySQL-sourced batch processed | ≤ 24 hours | Pipeline run |
| Classification quality | ≥ 90% | Human audit |
| P95 latency at 1K concurrent | ≤ 3s | Locust report |
| Query API P95 latency | ≤ 200ms | Locust report |
| Cost projection accuracy | Within 20% | Billing dashboard |
| All critical alerts fire in staging | 100% | Alert runbook test |

**Go:** All green → begin production ramp.
**No-Go:** Any red → 1-week remediation sprint → re-test before ramp.

### 4.4 Production Ramp Plan

```
Week 6:   10M URLs/day   — monitor cost, latency, error rate hourly
Week 7:   100M URLs/day  — verify auto-scaling headroom, no hot spots
Week 8:   500M URLs/day  — full observability review, ops handover prep
Week 10:  1B URLs/day    — full production, SLA active, on-call rotation live
```

### 4.5 Release Quality Checklist

- **Code review:** All PRs reviewed by ≥ 1 senior engineer, no self-merges
- **Test coverage:** ≥ 80% on `crawler.py` and `classifier.py` (pytest-cov)
- **CI pipeline:** GitHub Actions runs `pytest` on every push; deploy blocked if tests fail
- **Secrets management:** API keys in GCP Secret Manager / AWS Secrets Manager; never in code or `.env` committed to git
- **Rollback plan:** Cloud Run traffic splitting — 10% canary for 30 min before 100% rollout
- **Incident runbook:** Written procedures for top-5 failure modes:
  1. Queue backlog exceeds 5M messages
  2. Crawl success rate drops below 80%
  3. Monthly cost 2× projected budget
  4. All target domains returning 403
  5. Classifier degraded to keywords-only (AI APIs down)

---

## 5. Repository Structure

```
brightedge-crawler/
├── main.py              # FastAPI app — /crawl (write) + /metadata /search (read)
├── crawler.py           # URL normalization + HTML fetch + metadata extraction
├── classifier.py        # Claude → Gemini → keyword topic classification (3-layer)
├── test_crawler.py      # Pytest unit tests (all HTTP mocked — no real network calls)
├── Dockerfile           # Container for EC2 / Cloud Run
├── cloudbuild.yaml      # GCP Cloud Build + Cloud Run CI/CD
├── requirements.txt     # Pinned dependencies
├── .env.example         # Template for local secrets (never commit real keys)
├── .gitignore
└── README.md           
```

