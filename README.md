# BrightEdge Web Crawler — Engineering Assignment

> **AI Tools Used:** Claude API (Anthropic) for page topic classification; Claude claude-sonnet-4-20250514 (chat) used for design consultation and code review during development.

## GitHub — publish this repo

1. Create a **new empty repository** on GitHub (no README/license if you already have them here).
2. On your machine, from this project folder:

   ```bash
   git init
   git add .
   git commit -m "Initial commit: FastAPI crawler + classifier"
   git branch -M main
   git remote add origin https://github.com/YOUR_USER/YOUR_REPO.git
   git push -u origin main
   ```

3. **Do not commit API keys.** This repo uses `.gitignore` for `.env` and `venv/`. Use `.env.example` as a template only.
4. **CI:** Pushes to `main` / `master` run `pytest` via [`.github/workflows/ci.yml`](.github/workflows/ci.yml) (no cloud keys required).

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Part 1 — Core Crawler: Setup & API Usage](#2-part-1--core-crawler)
3. [Part 2 — Scale Architecture Design](#3-part-2--scale-architecture-design)
4. [Part 3 — PoC Roadmap & Release Plan](#4-part-3--poc-roadmap--release-plan)
5. [Repository Structure](#5-repository-structure)

---

## 1. Project Overview

This service crawls any given URL, extracts structured HTML metadata (title, description, Open Graph tags, headings, body text), and classifies topics in order: **Claude** (if `ANTHROPIC_API_KEY` is set), else **Gemini** (if `GEMINI_API_KEY` is set), else **keyword taxonomy** (always available).

**Tech stack:** Python 3.12+ · FastAPI · BeautifulSoup4 · Anthropic SDK · `requests` · Docker · optional GCP Cloud Run (`cloudbuild.yaml`)

---

## 2. Part 1 — Core Crawler

### 2.1 Local Setup

```bash
# Clone and enter the repo
git clone https://github.com/<your-username>/<your-repo>.git
cd <your-repo>

# Create a virtual environment
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Optional API keys (keyword fallback works without either)
export ANTHROPIC_API_KEY="sk-ant-..."
export GEMINI_API_KEY="AIza..."

# Run the server (repo root — not app/)
python main.py
# → http://localhost:8080  ·  API docs at http://localhost:8080/docs

# Tests
pip install pytest && pytest test_crawler.py -v
```

### 2.2 API Reference

#### `POST /crawl`

```bash
curl -X POST https://<your-cloud-run-url>/crawl \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.amazon.com/Cuisinart-CPT-122-Compact-2-Slice-Toaster/dp/B009GQ034C/"}'
```

#### `GET /crawl` (quick test)

```bash
curl "https://<your-cloud-run-url>/crawl?url=https://blog.rei.com/camp/how-to-introduce-your-indoorsy-friend-to-the-outdoors/"
```

### 2.3 Example Response

```json
{
  "url": "https://www.amazon.com/Cuisinart-CPT-122-Compact-2-Slice-Toaster/dp/B009GQ034C/",
  "status_code": 200,
  "canonical": "https://www.amazon.com/dp/B009GQ034C",
  "title": "Cuisinart CPT-122 Compact 2-Slice Toaster, Brushed Chrome",
  "description": "Buy Cuisinart CPT-122 Compact 2-Slice Toaster...",
  "keywords": ["toaster", "cuisinart", "kitchen appliances"],
  "og_tags": {
    "og:type": "product",
    "og:title": "Cuisinart CPT-122 Compact 2-Slice Toaster"
  },
  "headings": {
    "h1": ["Cuisinart CPT-122 Compact 2-Slice Toaster"],
    "h2": ["Product details", "Customer reviews"],
    "h3": []
  },
  "body_snippet": "Cuisinart CPT-122 Compact 2-Slice Toaster. Wide slots accommodate thick breads...",
  "topics": [
    "Kitchen Appliances > Toasters",
    "E-Commerce > Amazon Product Page",
    "Small Kitchen Appliances",
    "Home & Kitchen"
  ],
  "classification_source": "claude",
  "error": null
}
```

### 2.4 GCP Cloud Run Deployment

```bash
# One-time setup
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com

# Deploy (replace with your actual API key)
gcloud builds submit --config cloudbuild.yaml \
  --substitutions _ANTHROPIC_API_KEY="sk-ant-..."

# Your service URL will be printed at the end:
# → Service URL: https://brightedge-crawler-xxxx-uc.a.run.app
```

**Why Cloud Run?**
- **Free tier:** 2M requests/month, 360K GB-seconds compute — more than enough for a demo
- **Scale to zero:** No idle costs; container spins up on first request
- **Fully managed:** No Kubernetes or VM management needed

---

## 3. Part 2 — Scale Architecture Design

### 3.1 The Challenge

Crawling **billions of URLs** is not a single-machine problem. At that scale we face:

| Challenge | Impact |
|-----------|--------|
| Network I/O is slow (~100-500ms per page) | Need massive parallelism |
| Pages fail transiently | Need retries with backoff |
| Duplicate URLs | Waste money if re-crawled |
| Cost of LLM classification per URL | Must batch / cache aggressively |
| Storage of billions of metadata records | Need columnar / partitioned storage |

### 3.2 High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         INGESTION LAYER                             │
│                                                                     │
│   Text File (billions of URLs)   MySQL table (url_queue)           │
│          │                              │                           │
│          └──────────────┬───────────────┘                          │
│                         ▼                                           │
│              [URL Ingestion Service]                                │
│        Reads in chunks of 10,000 · deduplicates via                │
│        Redis BLOOM FILTER · publishes to Pub/Sub topic             │
└─────────────────────────────┬───────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         QUEUE LAYER                                 │
│                                                                     │
│              Google Cloud Pub/Sub  "urls-to-crawl"                 │
│         (durable, at-least-once, up to 7-day retention)            │
│         Partitioned by domain to respect rate limits               │
└────────────────────────┬────────────────────────────────────────────┘
                         │  (parallel pull)
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│  Crawler     │ │  Crawler     │ │  Crawler     │  ← Cloud Run
│  Worker #1   │ │  Worker #2   │ │  Worker #N   │     (autoscaled
│              │ │              │ │              │      0–100 instances)
│ fetch HTML   │ │ fetch HTML   │ │ fetch HTML   │
│ parse meta   │ │ parse meta   │ │ parse meta   │
│ classify     │ │ classify     │ │ classify     │
└──────┬───────┘ └──────┬───────┘ └──────┬───────┘
       └────────────────┼────────────────┘
                        ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        STORAGE LAYER                                │
│                                                                     │
│   BigQuery (crawl_metadata table)   ← analytical queries           │
│   GCS (raw HTML archive)            ← cheap long-term storage      │
│   Redis (crawl status cache)        ← dedup + rate limiting        │
└─────────────────────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     OBSERVABILITY LAYER                             │
│                                                                     │
│   Cloud Monitoring (metrics)  · Cloud Logging (structured logs)    │
│   Alerting (PagerDuty/Slack)  · Grafana dashboard                  │
└─────────────────────────────────────────────────────────────────────┘
```

### 3.3 Data Schema (BigQuery)

**Table: `crawl_metadata`** — partitioned by `crawl_date`, clustered by `domain`

```sql
CREATE TABLE crawl_metadata (
  url               STRING    NOT NULL,
  domain            STRING    NOT NULL,          -- extracted for clustering
  crawl_date        DATE      NOT NULL,          -- partition key (July 2025, etc.)
  crawl_timestamp   TIMESTAMP NOT NULL,
  status_code       INT64,
  canonical         STRING,
  title             STRING,
  description       STRING,
  keywords          ARRAY<STRING>,
  topics            ARRAY<STRING>,               -- AI-classified
  classification_source STRING,                  -- 'claude' | 'keywords' | 'none'
  og_type           STRING,                      -- og:type for page-type analysis
  h1_first          STRING,                      -- first H1 (most important heading)
  body_snippet      STRING,
  crawl_duration_ms INT64,                       -- performance tracking
  error             STRING,                      -- null on success
  raw_html_gcs_path STRING                       -- pointer to full HTML in GCS
)
PARTITION BY crawl_date
CLUSTER BY domain;
```

**Why BigQuery?**
- Handles petabyte-scale datasets with SQL
- Partitioning by date means queries for "July URLs" scan only that partition — **massive cost saving**
- Clustering by domain speeds up domain-level analytical queries

### 3.4 Deduplication Strategy

At billions of URLs, we must avoid re-crawling:

```
Before publishing a URL to Pub/Sub, check Redis:
  EXISTS crawled:{sha256(url)}
  → If YES: skip (already crawled this month)
  → If NO:  publish to queue, SET crawled:{sha256(url)} EX 2592000 (30-day TTL)

Bloom Filter (space-efficient):
  A Bloom filter for 1B URLs with 1% false positive rate needs ~1.2GB RAM.
  This lives in Redis and provides O(1) membership checks with minimal memory.
```

### 3.5 Rate Limiting per Domain

Responsible crawling means not hammering any single domain:

- Group Pub/Sub messages by domain using **message ordering keys**
- Each domain gets max **1 request/second** (configurable per domain in a config table)
- Exponential backoff on 429/503 responses: `wait = min(2^attempt * 0.5s, 60s)`

### 3.6 Cost Optimization

| Lever | Strategy |
|-------|----------|
| LLM classification | Batch 10 URLs per Claude call; cache results for duplicate titles |
| Storage | Compress raw HTML in GCS with gzip (~70% size reduction) |
| Compute | Cloud Run scales to zero; pay only per crawl-second |
| BigQuery | Partition + cluster queries; avoid full table scans |
| Retries | Dead-letter queue for persistently failing URLs; don't retry indefinitely |

**Rough cost estimate at 1B URLs/month:**
- Cloud Run crawling: ~$200-400/month (at 200ms avg per page)
- Claude API (batched, 1B pages): ~$500-800/month
- BigQuery storage: ~$20/month (10TB at $0.02/GB)
- GCS (compressed HTML): ~$100/month
- **Total: ~$850–1,300/month** — extremely competitive vs custom infrastructure

### 3.7 SLOs and SLAs

| Metric | SLO Target | Measurement Window |
|--------|-----------|-------------------|
| Crawl success rate | ≥ 95% of submitted URLs result in metadata | Per 24-hour batch |
| P95 crawl latency | ≤ 3 seconds per URL | Rolling 1-hour window |
| Classification accuracy | ≥ 90% topic relevance (human sample audit) | Weekly audit of 1,000 samples |
| API availability (query endpoint) | 99.9% uptime | Monthly |
| Data freshness | Crawled metadata available in BigQuery ≤ 4 hours after crawl | Per batch |
| Queue backlog SLO | Pub/Sub backlog cleared within 24 hours of ingestion | Per daily batch |

**SLA (contractual, customer-facing):**
- Query API: 99.5% uptime, < 500ms P95 response time
- Crawl pipeline: 95% of URLs processed within 48 hours of submission
- Breach compensation: service credits per standard GCP SLA model

### 3.8 Key Monitoring Metrics

**Infrastructure (Cloud Monitoring):**
- `crawler/requests_total` — total crawls attempted (counter)
- `crawler/success_rate` — % successful vs failed (gauge)
- `crawler/latency_p50_p95_p99` — latency percentiles (histogram)
- `pubsub/subscription/num_undelivered_messages` — queue backlog depth
- `run/request_count` + `run/container/cpu/utilization` — Cloud Run health

**Business metrics (BigQuery dashboards):**
- URLs crawled per hour by domain / topic category
- Topic distribution changes over time (SEO trend analysis)
- Error rate breakdown by error type (timeout, 403, 404, parse failure)
- Classification source ratio (Claude vs keyword fallback)

**Alerting thresholds:**
```
CRITICAL: success_rate < 80% for 15min → PagerDuty
WARNING:  queue_backlog > 5M messages → Slack
WARNING:  p95_latency > 5s for 5min → Slack  
CRITICAL: classifier error rate > 20% → PagerDuty
```

---

## 4. Part 3 — PoC Roadmap & Release Plan

### 4.1 Engineering Task Breakdown

#### Milestone 1 — Working Crawler (Week 1) ✅
| Task | Est. | Owner |
|------|------|-------|
| Core crawler (`crawler.py`) | 1d | Backend |
| Claude classifier (`classifier.py`) | 0.5d | Backend |
| FastAPI service (`main.py`) | 0.5d | Backend |
| Docker + GCP deploy | 0.5d | DevOps |
| Manual testing on 5 sample URLs | 0.5d | QA |
| **Total** | **3d** | |

#### Milestone 2 — Scale Infrastructure (Weeks 2–3)
| Task | Est. | Owner |
|------|------|-------|
| URL ingestion service (file/MySQL reader → Pub/Sub) | 2d | Backend |
| Bloom filter dedup (Redis) | 1d | Backend |
| Domain rate limiting | 1d | Backend |
| BigQuery schema + table creation | 1d | Data Eng |
| GCS raw HTML archiving | 0.5d | Backend |
| Worker autoscaling config (Cloud Run) | 0.5d | DevOps |
| **Total** | **6d** | |

#### Milestone 3 — Observability & Hardening (Week 4)
| Task | Est. | Owner |
|------|------|-------|
| Structured logging to Cloud Logging | 1d | Backend |
| Cloud Monitoring dashboards | 1d | DevOps |
| Alerting policies (critical thresholds) | 0.5d | DevOps |
| Retry + dead-letter queue logic | 1d | Backend |
| Load test at 10K URLs | 1d | QA |
| **Total** | **4.5d** | |

#### Milestone 4 — PoC Validation (Week 5)
| Task | Est. | Owner |
|------|------|-------|
| End-to-end test: 1M URL batch | 2d | QA + Eng |
| Cost audit (actual vs projected) | 0.5d | Eng |
| Human audit of classification quality (1K samples) | 1d | Product |
| Performance tuning based on load test findings | 1d | Backend |
| Documentation finalization | 0.5d | All |
| **Total** | **5d** | |

**Total PoC timeline: ~4–5 weeks** with a team of 3–4 engineers.

### 4.2 Potential Blockers

#### Known & Solvable
| Blocker | Mitigation |
|---------|-----------|
| **Bot detection / 403s** | Rotate User-Agents, add request delays, use residential proxies for priority domains |
| **JavaScript-rendered pages** (SPAs) | Add optional Playwright/Puppeteer headless browser path for JS-heavy domains |
| **Pub/Sub message ordering at scale** | Use domain as ordering key; pre-shard topics by domain hash |
| **Claude API rate limits** | Implement exponential backoff + request batching (10 URLs per call) |
| **BigQuery write quotas** | Use streaming inserts with BQSF (BigQuery Storage Write API) for higher throughput |

#### Unknown Risks
| Risk | Probability | Impact | Mitigation |
|------|------------|--------|-----------|
| Domain IP blocks at high volume | Medium | High | Implement per-domain crawl rate config |
| Claude API pricing changes | Low | Medium | Keep keyword fallback operational at all times |
| URL charset / encoding edge cases | High | Low | Use `chardet` for encoding detection |
| Pages with anti-scraping legal notices | Medium | High | Legal review of ToS for target domains |

### 4.3 Release Plan

#### PoC Success Criteria
Before promoting to production, the PoC must satisfy **all** of the following:

- [ ] Successfully crawls all 5 sample URLs from the assignment spec
- [ ] Processes 1M URL batch within 24 hours
- [ ] Crawl success rate ≥ 95% on clean URL list
- [ ] Classification relevance ≥ 90% (human audit of 1,000 random samples)
- [ ] P95 latency ≤ 3s per URL under 1,000 concurrent requests
- [ ] Monthly cost projection stays within approved budget
- [ ] All critical alerts fire correctly in staging environment
- [ ] Runbook written for top 5 failure modes

#### Go/No-Go Decision Gate
After the 1M URL load test (end of Week 4), Engineering + Product hold a 30-minute Go/No-Go review:
- **Go:** All success criteria met → proceed to full-scale production ramp (10B URLs/month)
- **No-Go:** Any blocking criteria failed → 1-week remediation sprint → re-test

#### Production Ramp Plan
```
Week 6:  10M URLs/day  — monitor closely, verify cost matches projection
Week 7:  100M URLs/day — full observability checks
Week 8:  1B URLs/day   — full scale, handover to ops team
```

### 4.4 Quality Checklist for High-Quality Release

- **Code review:** All PRs reviewed by ≥ 1 senior engineer
- **Unit tests:** ≥ 80% code coverage on `crawler.py` and `classifier.py`
- **Integration tests:** Test suite runs against live sample URLs in CI
- **Secrets management:** API keys stored in GCP Secret Manager, never in code/env files
- **Rollback plan:** Cloud Run traffic splitting — new version gets 10% traffic; full rollout after 30min clean run
- **Incident runbook:** Documented steps for top-5 failure modes (queue backlog, high error rate, cost spike, IP block, classifier outage)

---

## 5. Repository Structure

```
brightedge-crawler/
├── main.py              # FastAPI app — routes, request/response models
├── crawler.py           # HTML fetching + metadata extraction
├── classifier.py        # Claude → Gemini → keyword topic classification
├── test_crawler.py      # Pytest unit tests (mocked HTTP)
├── Dockerfile           # Container for Cloud Run / any Docker host
├── cloudbuild.yaml      # GCP Cloud Build + Cloud Run deploy
├── .github/workflows/ci.yml  # GitHub Actions — pytest on push/PR
├── requirements.txt
├── .env.example         # Template for local secrets (do not put real keys in git)
└── README.md
```

---

*Built for the BrightEdge Software Engineering assignment.*
*AI assistance: Claude API (topic classification), Claude claude-sonnet-4-20250514 (development consultation).*
