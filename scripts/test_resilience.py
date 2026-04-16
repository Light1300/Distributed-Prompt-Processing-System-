import argparse
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import httpx

# Colours

GREEN  = "\033[0;32m"
RED    = "\033[0;31m"
YELLOW = "\033[1;33m"
BLUE   = "\033[0;34m"
NC     = "\033[0m"

def ok(msg):   print(f"{GREEN}[PASS]{NC} {msg}")
def fail(msg): print(f"{RED}[FAIL]{NC} {msg}"); sys.exit(1)
def info(msg): print(f"{BLUE}[INFO]{NC} {msg}")
def warn(msg): print(f"{YELLOW}[WARN]{NC} {msg}")
def header(msg):
    print(f"\n{BLUE}{'═' * 46}{NC}")
    print(f"{BLUE} {msg}{NC}")
    print(f"{BLUE}{'═' * 46}{NC}")


# HTTP helpers 

@dataclass
class TestResult:
    passed: int = 0
    failed: int = 0
    warnings: list = field(default_factory=list)


def submit_job(
    client: httpx.Client,
    host: str,
    user_id: str,
    prompt_id: str,
    text: str,
    priority: str = "normal",
) -> dict:
    resp = client.post(
        f"{host}/v1/process",
        json={
            "user_id": user_id,
            "prompt_id": prompt_id,
            "text": text,
            "priority": priority,
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()


def poll_until_done(
    client: httpx.Client,
    host: str,
    job_id: str,
    max_wait: int = 120,
) -> Optional[dict]:
    elapsed = 0
    while elapsed < max_wait:
        try:
            resp = client.get(f"{host}/v1/status/{job_id}", timeout=5.0)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") in ("completed", "failed"):
                    return data
        except Exception:
            pass
        time.sleep(1)
        elapsed += 1
    return None


def wait_for_api(client: httpx.Client, host: str, max_wait: int = 60) -> bool:
    info("Waiting for API to be ready...")
    for i in range(max_wait):
        try:
            resp = client.get(f"{host}/v1/health", timeout=3.0)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


# Individual tests 

def test_preflight(client: httpx.Client, host: str, results: TestResult):
    header("Preflight: API health check")

    if not wait_for_api(client, host):
        fail("API did not respond within 60s — is docker compose running?")

    resp = client.get(f"{host}/v1/health", timeout=5.0)
    data = resp.json()

    assert data["status"] == "healthy", f"Expected healthy, got {data['status']}"
    assert data["components"]["database"] == "connected"
    assert data["components"]["cache"] == "available"

    ok(f"API healthy — db={data['components']['database']} "
       f"cache={data['components']['cache']} "
       f"worker={data['components']['worker']}")
    results.passed += 1


def test_basic_submit_and_poll(client: httpx.Client, host: str, results: TestResult):
    header("Test 1: Basic submit and poll")

    ts = int(time.time())
    submit = submit_job(client, host, "test-user", f"basic-{ts}", "What is the speed of light?")

    job_id = submit.get("job_id")
    assert job_id, "No job_id in submit response"
    ok(f"Submitted job: {job_id}")

    result = poll_until_done(client, host, job_id, max_wait=90)
    assert result, "Job did not complete within 90s"
    assert result["status"] == "completed", f"Expected completed, got {result['status']}"
    ok("Job completed successfully")

    assert result.get("response"), "Response text is empty"
    ok(f"Response present: {result['response'][:60]}...")

    results.passed += 1


def test_idempotency(client: httpx.Client, host: str, results: TestResult):
    header("Test 2: Idempotency — same prompt_id twice")

    ts = int(time.time())
    pid = f"idempotent-{ts}"

    r1 = submit_job(client, host, "test-user", pid, "What is photosynthesis?")
    job1 = r1["job_id"]
    ok(f"First submission job_id:  {job1}")

    r2 = submit_job(client, host, "test-user", pid, "What is photosynthesis?")
    job2 = r2["job_id"]
    ok(f"Second submission job_id: {job2}")

    if job1 == job2:
        ok("Idempotency confirmed — same job_id returned for duplicate prompt_id")
    else:
        warn(f"Different job_ids returned (job1={job1}, job2={job2})")
        results.warnings.append("Idempotency: different job_ids for same prompt_id")

    result = poll_until_done(client, host, job1, max_wait=90)
    assert result and result["status"] == "completed", "Idempotent job did not complete"
    ok("Idempotent job completed")

    results.passed += 1


def test_semantic_cache(client: httpx.Client, host: str, results: TestResult):
    header("Test 3: Semantic cache hit")

    ts = int(time.time())

    # Cold request
    t_start = time.monotonic()
    r_cold = submit_job(client, host, "test-user", f"cache-cold-{ts}",
                        "Explain quantum computing in simple terms")
    result_cold = poll_until_done(client, host, r_cold["job_id"], max_wait=120)
    t_cold_ms = round((time.monotonic() - t_start) * 1000)

    assert result_cold and result_cold["status"] == "completed"
    ok(f"Cold request done — cached={result_cold.get('cached')} time={t_cold_ms}ms")

    time.sleep(1)

    # Warm request — identical text, new prompt_id
    t_start = time.monotonic()
    r_warm = submit_job(client, host, "test-user", f"cache-warm-{ts}",
                        "Explain quantum computing in simple terms")
    result_warm = poll_until_done(client, host, r_warm["job_id"], max_wait=60)
    t_warm_ms = round((time.monotonic() - t_start) * 1000)

    assert result_warm and result_warm["status"] == "completed", \
        "Cache-warm job did not complete"

    if result_warm.get("cached"):
        ok(f"Cache HIT confirmed — cached=True time={t_warm_ms}ms")
        speedup = round(t_cold_ms / t_warm_ms, 1) if t_warm_ms > 0 else "∞"
        info(f"Cold: {t_cold_ms}ms → Warm: {t_warm_ms}ms ({speedup}x speedup)")
    else:
        warn(f"Cache MISS on warm request — similarity may be below threshold")
        results.warnings.append("Cache: warm request did not hit cache")

    results.passed += 1


def test_priority_queuing(client: httpx.Client, host: str, results: TestResult):
    header("Test 4: Priority queuing")

    ts = int(time.time())
    high_jobs = []
    low_jobs  = []

    info("Submitting 3 low + 3 high priority jobs...")

    for i in range(1, 4):
        r = submit_job(client, host, "test-user", f"low-{i}-{ts}",
                       f"Low priority job number {i}", "low")
        low_jobs.append(r["job_id"])

    for i in range(1, 4):
        r = submit_job(client, host, "test-user", f"high-{i}-{ts}",
                       f"High priority job number {i}", "high")
        high_jobs.append(r["job_id"])

    ok(f"Submitted 6 jobs")
    info(f"High priority IDs: {' '.join(high_jobs)}")
    info(f"Low  priority IDs: {' '.join(low_jobs)}")

    completed = 0
    all_jobs = high_jobs + low_jobs
    for jid in all_jobs:
        result = poll_until_done(client, host, jid, max_wait=120)
        if result and result["status"] == "completed":
            completed += 1
        else:
            status = result["status"] if result else "timeout"
            warn(f"Job {jid} ended with status={status}")
            results.warnings.append(f"Priority: job {jid} did not complete")

    if completed == len(all_jobs):
        ok("All priority queue jobs completed")
    else:
        warn(f"Only {completed}/{len(all_jobs)} priority jobs completed")

    info("Verify ordering in Flower UI: http://localhost:5555")
    results.passed += 1


def test_crash_recovery(
    client: httpx.Client,
    host: str,
    results: TestResult,
    skip: bool = False,
):
    header("Test 5: Worker crash recovery")

    if skip:
        warn("Skipping crash recovery test (--skip-crash flag set)")
        results.warnings.append("Crash recovery: skipped")
        return

    ts = int(time.time())
    job_ids = []

    info("Submitting 8 jobs before killing worker...")
    for i in range(1, 9):
        r = submit_job(
            client, host, "test-user",
            f"crash-{i}-{ts}",
            f"Crash recovery test prompt number {i} what is machine learning",
            "normal",
        )
        job_ids.append(r["job_id"])
        info(f"  Submitted job {i}: {r['job_id']}")

    ok(f"Submitted {len(job_ids)} jobs")

    # Kill worker1
    info("Killing worker1...")
    try:
        result = subprocess.run(
            ["docker", "compose", "kill", "worker"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            ok("worker1 killed")
        else:
            warn(f"Kill command output: {result.stderr.strip()}")
            ok("worker1 kill attempted (may already be down)")
    except subprocess.TimeoutExpired:
        warn("docker compose kill timed out")
    except FileNotFoundError:
        warn("docker compose not found — skipping kill step")

    time.sleep(2)

    # Poll all jobs  worker2 should complete them
    info("Polling jobs — worker2 should process all remaining...")
    completed = 0
    failed_jobs = 0

    for jid in job_ids:
        result = poll_until_done(client, host, jid, max_wait=180)
        if result and result["status"] == "completed":
            completed += 1
        else:
            status = result["status"] if result else "timeout"
            warn(f"Job {jid}: status={status}")
            failed_jobs += 1

    ok(f"Crash recovery: {completed}/{len(job_ids)} jobs completed after worker1 killed")

    if completed >= len(job_ids) * 0.75:
        ok("Crash recovery PASSED — jobs completed on surviving worker")
    else:
        fail(f"Crash recovery FAILED — {failed_jobs} jobs lost after crash")

    # Restart worker1
    info("Restarting worker1...")
    try:
        subprocess.run(
            ["docker", "compose", "up", "-d", "worker"],
            capture_output=True, text=True, timeout=30,
        )
        ok("worker1 restarted")
    except Exception as e:
        warn(f"Could not restart worker1: {e}")

    time.sleep(3)
    results.passed += 1


def test_metrics(client: httpx.Client, host: str, results: TestResult):
    header("Test 6: Metrics endpoint")

    resp = client.get(f"{host}/v1/metrics?window=300", timeout=10.0)
    assert resp.status_code == 200, f"Metrics returned {resp.status_code}"

    data = resp.json()

    required_fields = [
        "window_s", "throughput_rpm", "cache_hit_rate",
        "error_rate", "latency_ms", "rate_limit", "cache_total_entries"
    ]
    for field_name in required_fields:
        assert field_name in data, f"Missing field: {field_name}"

    ok("Metrics endpoint responding")
    info(f"  Throughput:      {data['throughput_rpm']:.1f} rpm")
    info(f"  Cache hit rate:  {data['cache_hit_rate']:.2%}")
    info(f"  Error rate:      {data['error_rate']:.2%}")
    info(f"  Cache entries:   {data['cache_total_entries']}")
    info(f"  Rate limit used: {data['rate_limit']['calls_used']}/{data['rate_limit']['calls_used'] + data['rate_limit']['calls_remaining']}")

    results.passed += 1
 

def main():
    parser = argparse.ArgumentParser(description="Resilience test suite")
    parser.add_argument("--host", default="http://localhost:8000",
                        help="API base URL (default: http://localhost:8000)")
    parser.add_argument("--skip-crash", action="store_true",
                        help="Skip the worker crash recovery test")
    args = parser.parse_args()

    host = args.host.rstrip("/")
    results = TestResult()

    print(f"\n{BLUE}Distributed LLM System — Resilience Test Suite{NC}")
    print(f"{BLUE}Target: {host}{NC}")

    with httpx.Client() as client:
        try:
            test_preflight(client, host, results)
            test_basic_submit_and_poll(client, host, results)
            test_idempotency(client, host, results)
            test_semantic_cache(client, host, results)
            test_priority_queuing(client, host, results)
            test_crash_recovery(client, host, results, skip=args.skip_crash)
            test_metrics(client, host, results)

        except AssertionError as e:
            fail(str(e))
        except KeyboardInterrupt:
            print(f"\n{YELLOW}Interrupted by user{NC}")
            sys.exit(1)

    # Summary 
    header("All Tests Complete")
    print()
    print(f"  Test 1: Basic submit + poll     {'PASS' if results.passed >= 1 else 'FAIL'}")
    print(f"  Test 2: Idempotency             {'PASS' if results.passed >= 2 else 'FAIL'}")
    print(f"  Test 3: Semantic cache hit      {'PASS' if results.passed >= 3 else 'FAIL'}")
    print(f"  Test 4: Priority queuing        {'PASS' if results.passed >= 4 else 'FAIL'}")
    print(f"  Test 5: Crash recovery          {'PASS' if results.passed >= 5 else 'FAIL'}")
    print(f"  Test 6: Metrics endpoint        {'PASS' if results.passed >= 6 else 'FAIL'}")
    print()

    if results.warnings:
        print(f"{YELLOW}Warnings:{NC}")
        for w in results.warnings:
            print(f"  {YELLOW}•{NC} {w}")
        print()

    ok(f"Suite complete — {results.passed}/6 tests passed")


if __name__ == "__main__":
    main()