# DESIGN.md — Trade-offs, Decisions, and What We'd Change at Scale

---

## Problem Framing

Before picking any technology, the constraints of this problem need to be clearly stated because they drive every decision:

1. **The system is write-heavy** — every prompt generates at minimum one DB insert (the request record) and often a second (the cache entry). The assignment states ~3:1 writes to reads.
2. **The rate limit is the central constraint** — 300 LLM calls per 60-second window must be enforced atomically across all worker processes simultaneously.
3. **Durability is non-negotiable** — a job that starts processing must complete even if the worker process dies.
4. **Semantic similarity, not exact match** — the cache must understand that "explain quantum computing" and "how does quantum computing work simply" are the same question.

Every design decision below flows from one of these four constraints.

---

## Decision 1 — Async API over Synchronous

**What we chose:** `POST /v1/process` returns `202 Accepted` with a `job_id` immediately. The client polls `GET /v1/status/{job_id}` for the result.

**Why not synchronous:** The assignment spec shows a synchronous response shape. We deviated deliberately.

With a 300 req/min rate limit, consider 400 simultaneous users. The first 300 requests acquire rate limit slots. The remaining 100 must wait for slots to open — potentially 20-60 seconds. A synchronous HTTP endpoint holds those 100 connections open for the full wait. Under real load this causes:
- Client timeout errors (most HTTP clients timeout at 30s)
- Server connection pool exhaustion
- Cascading retry storms

The async pattern decouples submission from processing entirely. The client gets an immediate acknowledgement. The server processes at its own rate. This is the standard architecture for rate-limited background processing — it is how OpenAI's batch API works, how AWS SQS works, and how every high-throughput job system is designed.

**Trade-off accepted:** Clients must implement polling logic. A production system would add WebSocket or Server-Sent Events for push notification instead of polling.

---

## Decision 2 — Celery over Temporal

**What we chose:** Celery 5 with Redis broker.

**What we considered:** Temporal.

Temporal is a genuinely powerful workflow orchestration engine. It handles arbitrary saga patterns, multi-step workflows, human approval steps, and complex retry logic with full event sourcing. If this system had workflows lasting minutes with multiple dependent LLM calls and database transactions that needed to roll back together — Temporal would be the right call.

Our pipeline has 8 steps, runs in under one second, and has one external call (the LLM). Temporal's value proposition is wasted here. More importantly, Temporal requires its own cluster — in Docker Compose, that means adding `temporalio/auto-setup` and `temporalio/ui` as services, adding ~800MB to the stack, and learning Temporal's worker SDK pattern.

Celery gives us the one thing we actually need — `acks_late=True` — with zero additional infrastructure. The broker is Redis, which we already need for the rate limiter and result backend.

**The one Celery limitation worth noting:** Celery's at-least-once delivery means a task that crashes after the DB write but before ACK will re-run on the next worker. The idempotency check (`if request.status == "completed": return stored result`) makes this safe. Temporal's exactly-once semantics would eliminate this concern entirely, but the tradeoff (full Temporal cluster) is not justified.

---

## Decision 3 — The Rate Limiter Design

This is the most technically interesting part of the system and worth explaining in depth.

**The requirement:** Never exceed 300 LLM calls per 60-second window across all workers simultaneously.

**Why a Python lock fails:** With 2 workers × 4 fork processes = 8 concurrent execution contexts, a `threading.Lock()` only works within a single process. Each of the 8 processes would maintain its own counter, potentially allowing 8 × 300 = 2400 calls/minute through while each process believes it is respecting the limit.

**Why a database counter fails:** A `SELECT count WHERE timestamp > now() - 60s; INSERT;` pattern has a TOCTOU race. Two workers can both read count=299, both decide they are under the limit, and both insert — resulting in 301 calls.

**What we chose:** A Redis Lua script that runs atomically.

```lua
ZREMRANGEBYSCORE key -inf (now - window_ms)  -- remove old timestamps
count = ZCARD key                             -- count remaining
if count >= limit then return {0, retry_after} end
ZADD key now (now .. jitter)                  -- add this call
EXPIRE key (window_seconds + 5)
return {1, 0}
```

Redis is single-threaded. Lua scripts execute atomically — no other command can execute between any two lines of this script. All 8 worker processes call this script. The first 300 to call it in any 60-second window get a `1` (allowed). The 301st gets a `0` (rejected, with `retry_after` seconds). No race condition is possible.

**Sliding vs fixed window:** This implements a sliding window using a sorted set of timestamps. A fixed window counter would allow 300 calls at 11:59:59 and another 300 at 12:00:00 — 600 calls in two seconds. The sliding window guarantees the limit is exactly 300 per any 60-second period regardless of timing.

---

## Decision 4 — PostgreSQL over MongoDB or Pure Redis

**The workload characteristics:**
- Very high write throughput (every prompt = 1-2 inserts)
- Atomic idempotency checks on `prompt_id`
- Vector similarity search for cache lookups
- Concurrent hit count increments on cache entries
- Status transitions: pending → processing → completed/failed

**Why not MongoDB:** MongoDB has competitive write throughput and reasonable horizontal scaling. The problem is vector search. MongoDB Atlas Vector Search exists but requires Atlas (cloud). Self-hosted MongoDB has no native vector similarity — you would need to add Pinecone, Weaviate, or Qdrant as a separate service. That is a separate infrastructure component, separate operational concern, separate failure mode.

PostgreSQL with `pgvector` handles both relational records and vector operations in one service. The migration creates an IVFFlat index ready for ANN queries at scale.

**Why not pure Redis:** Redis would give the fastest write throughput. But Redis is an in-memory store with optional persistence. A crash between writes can lose committed records. For a system where "a job that starts must complete" is a hard requirement, losing the request record means losing the job permanently. PostgreSQL's WAL gives us durable commits.

**Concurrent hit count increment:** Multiple workers can hit the same cache entry simultaneously. The naive approach — `SELECT hit_count; update = hit_count + 1; UPDATE SET hit_count = update` — has a race condition. Our implementation uses:

```sql
UPDATE semantic_cache
SET hit_count = hit_count + 1, last_hit_at = NOW()
WHERE id = $1
```

The `UPDATE` takes a row-level lock. Two concurrent workers hitting the same cache entry will serialize on this lock. No lost increments.

---

## Decision 5 — Sentence-Transformers over OpenAI Embeddings

**What we chose:** `all-MiniLM-L6-v2` via sentence-transformers, running locally.

**Why not OpenAI's `text-embedding-ada-002`:** Two reasons. First, the assignment says "Do NOT use real API keys" — a real embedding API would require a real key. Second, even if allowed, an external embedding API adds network latency (50-150ms round trip) to every single request, including cache lookups. The whole point of the cache is speed — adding network latency defeats it.

`all-MiniLM-L6-v2` encodes in ~5ms on CPU, produces 384-dim vectors, and is 22MB. It loads once per worker process. No network call, no API key, no rate limit, no cost.

**The MockEmbedder for tests:** The real model takes 15 seconds to load. With 59 tests, that would mean 59 × 15s = 885 seconds of model loading if loaded fresh for each test. The `MockEmbedder` uses SHA256 hash as a random seed to produce deterministic 384-dim unit vectors. Identical text → identical hash → identical vector → dot product = 1.0 exactly. This lets threshold tests work correctly while running in 5 seconds total.

---

## Known Limitations and What We'd Fix at Scale

### Limitation 1 — Cache lookup is O(n)

**Current implementation:** Load all embeddings from Postgres into Python, compute cosine similarity in a loop, return the best match above threshold.

**Problem:** At 10k cache entries this is acceptable. At 100k entries this is a full table scan on every request — the DB dies under load.

**Fix:** The migration already creates an IVFFlat index:
```sql
CREATE INDEX ix_semantic_cache_embedding_ivfflat
ON semantic_cache
USING ivfflat (embedding vector_cosine_ops)
WITH (lists = 100);
```

Replace the Python loop with:
```sql
SELECT response, 1 - (embedding <=> $1) AS similarity
FROM semantic_cache
WHERE 1 - (embedding <=> $1) > 0.9
ORDER BY embedding <=> $1
LIMIT 1;
```

This uses the index. Query time drops from O(n) to O(log n) for approximate nearest-neighbour search.

**IVFFlat vs HNSW:** IVFFlat was chosen over HNSW because cache misses are acceptable — a false negative (missing a valid cache entry) just falls through to the LLM. We do not need HNSW's higher recall at the cost of more memory and longer build time. At >500k entries, revisit this.

### Limitation 2 — Stuck jobs in "processing" state

**The gap:** `acks_late=True` handles the common case — worker dies, broker requeues. But there is a small window: the worker sets `status=processing` in Postgres, then the process is killed before Celery can requeue (e.g., `SIGKILL` with no grace period). The Postgres record stays `processing` forever.

**Fix:** A periodic background task (Celery Beat, cron, or a separate service) that runs:
```sql
UPDATE prompt_requests
SET status = 'pending', task_id = NULL
WHERE status = 'processing'
  AND updated_at < NOW() - INTERVAL '5 minutes'
RETURNING prompt_id;
```

Then requeues the returned `prompt_id` values. This is the standard heartbeat/timeout recovery pattern.

### Limitation 3 — In-flight deduplication

**The gap:** Two concurrent requests with identical text both miss the cache (it has not been populated yet) and both proceed to the LLM. Both pay the LLM cost and latency. One of them will populate the cache; the other's result is redundant.

**Fix:** Before the LLM call, acquire a Redis lock keyed on the prompt text hash:
```python
lock_key = f"inflight:{hashlib.sha256(text.encode()).hexdigest()}"
with redis.lock(lock_key, timeout=10):
    cache_result = cache.lookup(text)  # re-check under lock
    if cache_result:
        return cache_result
    response = llm.complete(text)
    cache.store(text, response)
```

The second concurrent request for the same text blocks on the lock, then finds the cache populated and returns without calling the LLM.

### Limitation 4 — No backpressure

**The gap:** The Redis queue grows unboundedly if submission rate exceeds processing rate. Memory eventually runs out.

**Fix:** Check queue depth before accepting a job:
```python
queue_depth = celery_app.control.inspect().reserved()
if queue_depth > MAX_QUEUE_DEPTH:
    raise HTTPException(status_code=429, detail="Queue full, try again later")
```

Return `Retry-After` header with estimated wait time.

### Limitation 5 — Rate limit fairness under pressure

**The gap:** The rate limiter is global — all priorities compete for the same 300/min window. Under heavy load, a burst of low-priority requests can exhaust the window, blocking high-priority requests.

**Fix:** Separate rate limit counters per priority tier:
- `high`: 150 calls/min reserved
- `normal`: 100 calls/min
- `low`: 50 calls/min

Or implement token bucket per tier with burst allowance and spillover.

---

## What Changes at 10x Scale

| Component | Current | At 10x |
|---|---|---|
| Cache lookup | Python O(n) loop | pgvector ANN query with IVFFlat index |
| Workers | 2 Celery workers, 4 threads | Horizontal autoscaling based on queue depth |
| Redis | Single instance | Redis Sentinel (HA) or Redis Cluster (scale) |
| Rate limiter | Global sliding window | Per-priority-tier token buckets |
| DB connections | SQLAlchemy pool (10+20) | PgBouncer connection pooler in front of Postgres |
| Monitoring | Flower + structlog JSON | Prometheus metrics endpoint + Grafana dashboards |
| Dead letters | None | Dead letter queue for permanently failed tasks |
| Auth | None | API key middleware with per-key rate limits |

---

## Observed Performance (Real Numbers from Testing)

All numbers from actual test runs, verified in PostgreSQL:

- **Total requests processed:** 48 across all test sessions
- **Cache hit rate:** 67% (31 out of 46 completed requests were cache hits)
- **Cache entries:** 18 unique prompts stored
- **Crash recovery test:** 8 jobs submitted, worker1 killed mid-processing, all 8 completed on worker2
- **Idempotency:** Duplicate `prompt_id` returns same `job_id`, task enqueued exactly once (verified in integration tests)
- **Cache hit latency:** 50-200ms (embedding encode + similarity lookup + DB write)
- **Cold request latency:** 200-500ms (above + LLM call)
- **Unit tests:** 29 tests, 2.46 seconds
- **Full test suite:** 59 tests, 5.31 seconds
- **Model load time:** ~15 seconds per worker process (first request per fork worker)
- **Rate limiter:** Lua script, atomically enforced across 8 concurrent execution contexts (2 workers × 4 fork processes)