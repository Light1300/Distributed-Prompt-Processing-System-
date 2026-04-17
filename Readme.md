# Prompt Processing System

A distributed, durable LLM request processing system built for high-volume AI workloads.

```
POST /v1/process  →  Redis queue  →  Celery worker  →  semantic cache / mock LLM  →  Postgres
GET  /v1/status/{job_id}  →  poll for result
```

---

## Quick Start

```bash
# 1. Clone and enter
git clone <your-repo>
cd prompt-processor

# 2. Start everything
docker compose up --build

# 3. Hit the API (new terminal)
curl -X POST http://localhost:8000/v1/process \
  -H "Content-Type: application/json" \
  -d '{"user_id":"u1","prompt_id":"p1","text":"Explain quantum computing simply","priority":"high"}'

# 4. Poll for result using the job_id from step 3
curl http://localhost:8000/v1/status/<job_id>
```

Services that come up:

| Service  | URL                        | Notes                        |
|----------|----------------------------|------------------------------|
| API      | http://localhost:8000      | FastAPI + OpenAPI docs /docs |
| Flower   | http://localhost:5555      | Celery monitoring (admin/admin) |
| Postgres | localhost:5432             | promptdb                     |
| Redis    | localhost:6379             |                              |

---

## Architecture

```
Client
  │
  ▼
FastAPI (uvicorn, 2 workers)
  │  POST /v1/process → persist request (status=pending) → enqueue Celery task
  │  GET  /v1/status/{job_id} → read from Postgres / Celery result backend
  │
  ▼
Redis (broker)
  ├── Queue: high    ← processed first
  ├── Queue: normal
  └── Queue: low     ← processed last
  │
  ▼
Celery Workers (worker + worker2)
  │  acks_late=True → task stays in broker until completion
  │  If worker dies → broker requeues → other worker picks up
  │
  ▼
Pipeline (per task):
  1. Mark request processing
  2. Idempotency check (already done? return stored result)
  3. Generate embedding (sentence-transformers all-MiniLM-L6-v2)
  4. Cache lookup (cosine similarity > 0.9 → return cached, skip LLM)
  5. Acquire rate-limit slot (Redis Lua, 300 calls/60s, shared across all workers)
  6. Call MockLLM (200-500ms latency, 5% failure, exponential backoff retry)
  7. Store in semantic cache + update request record
  8. Return result
  │
  ▼
Postgres (pgvector/pgvector:pg16)
  ├── prompt_requests  — request ledger, status, results
  └── semantic_cache   — embeddings + cached responses
```

---

## Technology Choices

### FastAPI
Async-native, Pydantic v2 validation, automatic OpenAPI docs, minimal boilerplate. `uvicorn[standard]` for ASGI.

### Celery + Redis broker
Celery's `acks_late=True` is the key: the broker doesn't remove a task until the worker ACKs it after successful completion. If a worker process is killed mid-task, the broker requeues automatically — this is crash recovery for free. Three named queues (`high`, `normal`, `low`) give us priority ordering with no extra code.

Redis was chosen as the broker (not RabbitMQ) because it already serves double duty as the rate-limiter sorted set and Celery result backend — one fewer infrastructure component.

### PostgreSQL + pgvector
Write-heavy workload (every prompt = at least one insert). Postgres handles this well with WAL batching. `pgvector` adds native vector cosine similarity via IVFFlat index — no separate vector store needed. `INSERT … ON CONFLICT DO NOTHING` gives atomic idempotency in one query.

### sentence-transformers (all-MiniLM-L6-v2)
384-dim vectors, 22MB model, ~5ms encode on CPU. No external API call — deterministic, zero latency variance, no cost. Model is pre-baked into the Docker image at build time so first requests are not slow.

### Redis Lua rate limiter
The rate limiter runs a Lua script atomically on Redis. This means multiple workers across multiple processes share one exact counter with no TOCTOU race — the script removes old timestamps, counts, rejects or admits, all in one atomic operation. A sliding window (not a fixed window) means the limit is genuinely 300/min, not 300 per clock-minute boundary.

---

## API Reference

### `POST /v1/process`
Submit a prompt. Returns `202 Accepted` immediately with a `job_id`.

**Request:**
```json
{
  "user_id": "u1",
  "prompt_id": "p1",
  "text": "Explain quantum computing simply",
  "priority": "high"
}
```

**Response (202):**
```json
{
  "job_id": "3fa85f64-...",
  "prompt_id": "p1",
  "user_id": "u1",
  "status": "pending",
  "poll_url": "/v1/status/3fa85f64-..."
}
```

### `GET /v1/status/{job_id}`
Poll for result. Returns immediately with current status.

**Response (completed):**
```json
{
  "user_id": "u1",
  "prompt_id": "p1",
  "status": "completed",
  "cached": true,
  "response": "Quantum computing uses...",
  "processing_time_ms": 45,
  "retry_count": 0
}
```

### `GET /v1/health`
Returns `200` if healthy, `503` if any critical component is down.

### `GET /v1/metrics?window=60`
Operational metrics for the last N seconds.

---

## Running Tests

```bash
# Unit tests only (no infrastructure needed)
docker compose run --rm api pytest tests/unit/ -v

# All tests
docker compose run --rm api pytest -v

# With coverage
docker compose run --rm api pytest --cov=src --cov-report=term-missing
```

## Resilience Testing

```bash
# Full crash recovery + cache + idempotency test suite
./scripts/test_resilience.sh

# Load test (100 requests, 20 concurrent)
python scripts/load_test.py --total 100 --concurrency 20
```

---

## Project Structure

```
src/
├── api/
│   ├── main.py          # FastAPI app, lifespan, middleware, exception handlers
│   └── routes/
│       ├── process.py   # POST /v1/process
│       ├── status.py    # GET  /v1/status/{job_id}
│       ├── health.py    # GET  /v1/health
│       └── metrics.py   # GET  /v1/metrics
├── worker/
│   ├── celery_app.py    # Celery config, 3 priority queues, acks_late
│   └── tasks.py         # process_prompt — full 8-step pipeline
├── services/
│   ├── llm.py           # MockLLM + Redis Lua rate limiter
│   ├── cache.py         # Semantic cache — cosine similarity lookup + store
│   └── embeddings.py    # sentence-transformers wrapper + deterministic mock
├── models/
│   ├── db.py            # SQLAlchemy ORM models
│   └── schemas.py       # Pydantic request/response schemas
└── core/
    ├── config.py         # pydantic-settings — all config in one place
    ├── database.py       # async SQLAlchemy engine + session factory
    └── logging.py        # structlog JSON logging

tests/
├── unit/
│   ├── test_llm.py       # MockLLM rate limiting, failure rate
│   ├── test_cache.py     # Similarity threshold, hit counting
│   └── test_embeddings.py # Determinism, normalization, bounds
└── integration/
    └── test_api.py        # Schema validation, pipeline logic, response shapes

scripts/
├── test_resilience.sh    # Crash recovery + idempotency + cache test
└── load_test.py          # Concurrent load test with summary stats

alembic/
└── versions/
    └── 0001_initial_schema.py  # Creates both tables + indexes + pgvector
```

---

## Sample Commands

### Submitting and polling jobs

```bash
# Submit a high priority job
curl -X POST http://localhost:8000/v1/process \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "u1",
    "prompt_id": "p1",
    "text": "Explain quantum computing simply",
    "priority": "high"
  }' | python3 -m json.tool

# Submit a normal priority job
curl -X POST http://localhost:8000/v1/process \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "u1",
    "prompt_id": "p2",
    "text": "What is machine learning?",
    "priority": "normal"
  }' | python3 -m json.tool

# Submit a low priority job
curl -X POST http://localhost:8000/v1/process \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "u1",
    "prompt_id": "p3",
    "text": "How does the internet work?",
    "priority": "low"
  }' | python3 -m json.tool

# Poll for result — replace <job_id> with value from submit response
curl http://localhost:8000/v1/status/<job_id> | python3 -m json.tool

# Submit same text with different prompt_id — should return cached: true
curl -X POST http://localhost:8000/v1/process \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "u1",
    "prompt_id": "p4",
    "text": "Explain quantum computing simply",
    "priority": "normal"
  }' | python3 -m json.tool
```

### Testing idempotency

```bash
# Submit the same prompt_id twice — both return the same job_id
curl -X POST http://localhost:8000/v1/process \
  -H "Content-Type: application/json" \
  -d '{"user_id":"u1","prompt_id":"idem-1","text":"What is photosynthesis?"}' \
  | python3 -m json.tool

curl -X POST http://localhost:8000/v1/process \
  -H "Content-Type: application/json" \
  -d '{"user_id":"u1","prompt_id":"idem-1","text":"What is photosynthesis?"}' \
  | python3 -m json.tool

# Both responses will have the identical job_id
```

### Validation errors

```bash
# Missing required field — returns 422
curl -X POST http://localhost:8000/v1/process \
  -H "Content-Type: application/json" \
  -d '{"user_id":"u1","text":"hello"}' \
  | python3 -m json.tool

# Invalid priority value — returns 422
curl -X POST http://localhost:8000/v1/process \
  -H "Content-Type: application/json" \
  -d '{"user_id":"u1","prompt_id":"p1","text":"hello","priority":"urgent"}' \
  | python3 -m json.tool

# Invalid characters in user_id — returns 422
curl -X POST http://localhost:8000/v1/process \
  -H "Content-Type: application/json" \
  -d '{"user_id":"u1 with spaces","prompt_id":"p1","text":"hello"}' \
  | python3 -m json.tool
```

### Health and metrics

```bash
# Health check
curl http://localhost:8000/v1/health | python3 -m json.tool

# Metrics for last 60 seconds
curl http://localhost:8000/v1/metrics | python3 -m json.tool

# Metrics for last 5 minutes
curl http://localhost:8000/v1/metrics?window=300 | python3 -m json.tool

# Metrics for last hour
curl http://localhost:8000/v1/metrics?window=3600 | python3 -m json.tool
```

### Crash recovery demo

```bash
# Step 1 — submit a job
curl -X POST http://localhost:8000/v1/process \
  -H "Content-Type: application/json" \
  -d '{"user_id":"u1","prompt_id":"crash-demo","text":"What is the CAP theorem?"}' \
  | python3 -m json.tool

# Step 2 — kill worker1 immediately
docker compose kill worker

# Step 3 — poll for result — worker2 completes it
curl http://localhost:8000/v1/status/<job_id> | python3 -m json.tool

# Step 4 — restart worker1
docker compose up -d worker
```

---

## PostgreSQL — Inspecting the Database

Connect to the database directly:

```bash
docker exec -it rumikai-queuing-postgres-1 psql -U promptuser -d promptdb
```

Or run one-off queries without entering the shell:

```bash
docker exec rumikai-queuing-postgres-1 psql -U promptuser -d promptdb -c "<SQL here>"
```

### Check table structure

```bash
# List all tables
docker exec rumikai-queuing-postgres-1 psql -U promptuser -d promptdb -c "\dt"

# See prompt_requests column definitions
docker exec rumikai-queuing-postgres-1 psql -U promptuser -d promptdb -c "\d prompt_requests"

# See semantic_cache column definitions
docker exec rumikai-queuing-postgres-1 psql -U promptuser -d promptdb -c "\d semantic_cache"
```

### Inspect prompt_requests

```bash
# Most recent 10 requests
docker exec rumikai-queuing-postgres-1 psql -U promptuser -d promptdb -c "
SELECT prompt_id, user_id, status, cached, retry_count, processing_time_ms, created_at
FROM prompt_requests
ORDER BY created_at DESC
LIMIT 10;"

# All completed requests
docker exec rumikai-queuing-postgres-1 psql -U promptuser -d promptdb -c "
SELECT prompt_id, cached, processing_time_ms, created_at
FROM prompt_requests
WHERE status = 'completed'
ORDER BY created_at DESC;"

# All failed requests
docker exec rumikai-queuing-postgres-1 psql -U promptuser -d promptdb -c "
SELECT prompt_id, error, retry_count, created_at
FROM prompt_requests
WHERE status = 'failed'
ORDER BY created_at DESC;"

# Any stuck jobs (processing for more than 5 minutes)
docker exec rumikai-queuing-postgres-1 psql -U promptuser -d promptdb -c "
SELECT prompt_id, status, created_at, updated_at
FROM prompt_requests
WHERE status = 'processing'
AND updated_at < NOW() - INTERVAL '5 minutes';"

# Count by status
docker exec rumikai-queuing-postgres-1 psql -U promptuser -d promptdb -c "
SELECT status, COUNT(*) as count
FROM prompt_requests
GROUP BY status
ORDER BY count DESC;"

# Summary stats — total, cache hits, avg latency, error rate
docker exec rumikai-queuing-postgres-1 psql -U promptuser -d promptdb -c "
SELECT
  COUNT(*)                                              AS total_requests,
  SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
  SUM(CASE WHEN status = 'failed'    THEN 1 ELSE 0 END) AS failed,
  SUM(CASE WHEN cached = true        THEN 1 ELSE 0 END) AS cache_hits,
  ROUND(
    SUM(CASE WHEN cached = true THEN 1 ELSE 0 END)::numeric
    / NULLIF(SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END), 0) * 100, 2
  )                                                     AS cache_hit_pct,
  ROUND(AVG(processing_time_ms))                        AS avg_latency_ms,
  MIN(processing_time_ms)                               AS min_latency_ms,
  MAX(processing_time_ms)                               AS max_latency_ms
FROM prompt_requests;"

# Requests in the last 10 minutes
docker exec rumikai-queuing-postgres-1 psql -U promptuser -d promptdb -c "
SELECT prompt_id, status, cached, processing_time_ms
FROM prompt_requests
WHERE created_at > NOW() - INTERVAL '10 minutes'
ORDER BY created_at DESC;"
```

### Inspect semantic_cache

```bash
# All cache entries with hit counts
docker exec rumikai-queuing-postgres-1 psql -U promptuser -d promptdb -c "
SELECT prompt_text, hit_count, created_at, last_hit_at
FROM semantic_cache
ORDER BY hit_count DESC;"

# Most popular cache entries
docker exec rumikai-queuing-postgres-1 psql -U promptuser -d promptdb -c "
SELECT prompt_text, hit_count, created_at
FROM semantic_cache
WHERE hit_count > 0
ORDER BY hit_count DESC
LIMIT 10;"

# Total cache entries
docker exec rumikai-queuing-postgres-1 psql -U promptuser -d promptdb -c "
SELECT COUNT(*) AS total_cache_entries,
       SUM(hit_count) AS total_cache_hits,
       MAX(hit_count) AS max_hits_on_single_entry
FROM semantic_cache;"

# Cache entries never hit (hit_count = 0)
docker exec rumikai-queuing-postgres-1 psql -U promptuser -d promptdb -c "
SELECT prompt_text, created_at
FROM semantic_cache
WHERE hit_count = 0
ORDER BY created_at DESC;"

# Cache entries added in last hour
docker exec rumikai-queuing-postgres-1 psql -U promptuser -d promptdb -c "
SELECT prompt_text, hit_count, created_at
FROM semantic_cache
WHERE created_at > NOW() - INTERVAL '1 hour'
ORDER BY created_at DESC;"
```

## Known Limitations

- **Similarity lookup is O(n)** — the cache scans all embeddings in Python. At >50k entries, replace with a direct pgvector `ORDER BY embedding <=> $1 LIMIT 1` query to use the IVFFlat index.
- **No auth** — the API has no authentication. In production, add an API key middleware layer.
- **Synchronous polling** — clients must poll `/v1/status`. A production system would add WebSocket or Server-Sent Events for push notification.
- **Single Redis** — Redis is a single point of failure here. Production would use Redis Sentinel or Cluster.
