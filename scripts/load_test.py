import argparse
import asyncio
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

import httpx

API_BASE = "http://localhost:8000/v1"

PROMPT_POOL = [
    "Explain quantum computing in simple terms",
    "What is machine learning?",
    "How does the internet work?",
    "Explain quantum computing simply",        # near-duplicate → should cache hit
    "What is machine learning and how does it work?",  # near-duplicate
    "Describe how neural networks function",
    "What is the difference between AI and ML?",
    "How do transformers work in NLP?",
    "Explain the CAP theorem",
    "What is eventual consistency?",
]


@dataclass
class RequestResult:
    prompt_id: str
    submit_ms: float
    total_ms: Optional[float] = None
    status: str = "unknown"
    cached: bool = False
    error: Optional[str] = None


async def submit_and_poll(
    client: httpx.AsyncClient,
    prompt_text: str,
    semaphore: asyncio.Semaphore,
) -> RequestResult:
    prompt_id = f"load-{uuid.uuid4().hex[:12]}"
    result = RequestResult(prompt_id=prompt_id, submit_ms=0)
    wall_start = time.monotonic()

    async with semaphore:
        try:
            # Submit
            t0 = time.monotonic()
            submit_resp = await client.post(
                f"{API_BASE}/process",
                json={
                    "user_id": "load-tester",
                    "prompt_id": prompt_id,
                    "text": prompt_text,
                    "priority": "normal",
                },
                timeout=10.0,
            )
            result.submit_ms = (time.monotonic() - t0) * 1000

            if submit_resp.status_code != 202:
                result.status = "submit_error"
                result.error = f"HTTP {submit_resp.status_code}"
                return result

            job_id = submit_resp.json()["job_id"]

            # Poll until done
            for _ in range(60):
                await asyncio.sleep(0.5)
                poll_resp = await client.get(f"{API_BASE}/status/{job_id}", timeout=5.0)
                if poll_resp.status_code == 200:
                    data = poll_resp.json()
                    if data["status"] in ("completed", "failed"):
                        result.status = data["status"]
                        result.cached = data.get("cached", False)
                        result.error = data.get("error")
                        result.total_ms = (time.monotonic() - wall_start) * 1000
                        return result

            result.status = "timeout"
            result.total_ms = (time.monotonic() - wall_start) * 1000

        except Exception as e:
            result.status = "exception"
            result.error = str(e)
            result.total_ms = (time.monotonic() - wall_start) * 1000

    return result


async def run_load_test(concurrency: int, total: int, host: str):
    global API_BASE
    API_BASE = f"{host}/v1"

    print(f"\n{'═' * 50}")
    print(f" Load Test: {total} requests, {concurrency} concurrent")
    print(f" Target: {host}")
    print(f"{'═' * 50}\n")

    semaphore = asyncio.Semaphore(concurrency)
    prompts = [PROMPT_POOL[i % len(PROMPT_POOL)] for i in range(total)]

    async with httpx.AsyncClient() as client:
        # Check API is up
        try:
            health = await client.get(f"{host}/v1/health", timeout=5.0)
            if health.status_code != 200:
                print(f"⚠ API health check returned {health.status_code}")
        except Exception as e:
            print(f"✗ API unreachable: {e}")
            return

        start = time.monotonic()
        tasks = [submit_and_poll(client, prompt, semaphore) for prompt in prompts]
        results = await asyncio.gather(*tasks)
        elapsed = time.monotonic() - start

    # Summary 
    status_counts = Counter(r.status for r in results)
    completed = [r for r in results if r.status == "completed"]
    cached = [r for r in completed if r.cached]
    latencies = sorted([r.total_ms for r in completed if r.total_ms])

    def pct(data, p):
        if not data:
            return 0
        idx = int(len(data) * p / 100)
        return data[min(idx, len(data) - 1)]

    print(f"Results ({elapsed:.1f}s wall time):")
    print(f"  Total submitted:  {total}")
    print(f"  Completed:        {status_counts['completed']}")
    print(f"  Failed:           {status_counts.get('failed', 0)}")
    print(f"  Errors/timeouts:  {sum(v for k, v in status_counts.items() if k not in ('completed', 'failed'))}")
    print()
    print(f"Cache performance:")
    print(f"  Cache hits:       {len(cached)} / {len(completed)} ({len(cached)/max(len(completed),1):.1%})")
    print()
    if latencies:
        print(f"Latency (wall clock, includes polling):")
        print(f"  p50:  {pct(latencies, 50):.0f}ms")
        print(f"  p95:  {pct(latencies, 95):.0f}ms")
        print(f"  p99:  {pct(latencies, 99):.0f}ms")
    print()
    print(f"Throughput:")
    print(f"  Effective RPM:    {(len(completed) / elapsed * 60):.1f}")
    print(f"{'═' * 50}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--total", type=int, default=100)
    parser.add_argument("--host", default="http://localhost:8000")
    args = parser.parse_args()

    asyncio.run(run_load_test(args.concurrency, args.total, args.host))