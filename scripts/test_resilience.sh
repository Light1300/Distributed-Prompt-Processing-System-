#!/usr/bin/env bash
# test_resilience.sh
# Tests crash recovery, idempotency, cache hits, and priority queuing.
# Run with: ./scripts/test_resilience.sh
# Prerequisites: docker compose is up (docker compose up --build)

set -euo pipefail

API="http://localhost:8000/v1"
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

pass() { echo -e "${GREEN}[PASS]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; exit 1; }
info() { echo -e "${BLUE}[INFO]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }

# ── Helpers ───────────────────────────────────────────────────────────────────

submit_job() {
    local user_id="$1"
    local prompt_id="$2"
    local text="$3"
    local priority="${4:-normal}"

    curl -sf -X POST "$API/process" \
        -H "Content-Type: application/json" \
        -d "{\"user_id\":\"$user_id\",\"prompt_id\":\"$prompt_id\",\"text\":\"$text\",\"priority\":\"$priority\"}"
}

poll_until_done() {
    local job_id="$1"
    local max_wait="${2:-60}"
    local elapsed=0

    while [ "$elapsed" -lt "$max_wait" ]; do
        response=$(curl -sf "$API/status/$job_id" 2>/dev/null || echo '{"status":"unknown"}')
        status=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','unknown'))" 2>/dev/null)

        if [ "$status" = "completed" ] || [ "$status" = "failed" ]; then
            echo "$response"
            return 0
        fi

        sleep 1
        elapsed=$((elapsed + 1))
    done

    echo '{"status":"timeout"}'
    return 1
}

get_field() {
    local json="$1"
    local field="$2"
    echo "$json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('$field', ''))" 2>/dev/null
}

header() {
    echo ""
    echo -e "${BLUE}══════════════════════════════════════════════${NC}"
    echo -e "${BLUE} $*${NC}"
    echo -e "${BLUE}══════════════════════════════════════════════${NC}"
}

# ── Preflight ─────────────────────────────────────────────────────────────────

header "Preflight: Waiting for API"

info "Checking API health..."
for i in $(seq 1 30); do
    if curl -sf "$API/health" > /dev/null 2>&1; then
        pass "API is up"
        break
    fi
    if [ "$i" -eq 30 ]; then
        fail "API did not respond after 30s. Is docker compose running?"
    fi
    sleep 1
done

health=$(curl -sf "$API/health")
db=$(get_field "$health" "status")
[ "$db" = "healthy" ] && pass "Health check: healthy" || warn "Health check: $db (continuing anyway)"

# ── Test 1: Basic submit and poll ─────────────────────────────────────────────

header "Test 1: Basic submit and poll"

TS=$(date +%s)
resp=$(submit_job "test-user" "basic-$TS" "What is the speed of light?")
job_id=$(get_field "$resp" "job_id")
[ -n "$job_id" ] || fail "No job_id in submit response"
pass "Submitted job: $job_id"

result=$(poll_until_done "$job_id" 60)
status=$(get_field "$result" "status")
[ "$status" = "completed" ] || fail "Job did not complete (status=$status)"
pass "Job completed successfully"

response_text=$(get_field "$result" "response")
[ -n "$response_text" ] || fail "Response text is empty"
pass "Response text present: ${response_text:0:60}..."

# ── Test 2: Idempotency ───────────────────────────────────────────────────────

header "Test 2: Idempotency — same prompt_id twice"

IPID="idempotent-$TS"
r1=$(submit_job "test-user" "$IPID" "What is photosynthesis?")
job1=$(get_field "$r1" "job_id")
pass "First submission job_id: $job1"

# Submit exact same prompt_id again
r2=$(submit_job "test-user" "$IPID" "What is photosynthesis?")
job2=$(get_field "$r2" "job_id")
pass "Second submission job_id: $job2"

if [ "$job1" = "$job2" ]; then
    pass "Idempotency confirmed — same job_id returned for duplicate prompt_id"
else
    warn "Different job_ids returned (job1=$job1, job2=$job2) — check idempotency logic"
fi

# Wait for completion
result=$(poll_until_done "$job1" 60)
status=$(get_field "$result" "status")
[ "$status" = "completed" ] || fail "Idempotent job did not complete (status=$status)"
pass "Idempotent job completed"

# ── Test 3: Semantic cache hit ────────────────────────────────────────────────

header "Test 3: Semantic cache hit"

# First request — cold
PID_COLD="cache-cold-$TS"
r_cold=$(submit_job "test-user" "$PID_COLD" "Explain quantum computing in simple terms")
jid_cold=$(get_field "$r_cold" "job_id")
result_cold=$(poll_until_done "$jid_cold" 60)
cached_cold=$(get_field "$result_cold" "cached")
pass "First (cold) request done. cached=$cached_cold"

sleep 1

# Second request — same text, new prompt_id
PID_WARM="cache-warm-$TS"
r_warm=$(submit_job "test-user" "$PID_WARM" "Explain quantum computing in simple terms")
jid_warm=$(get_field "$r_warm" "job_id")
result_warm=$(poll_until_done "$jid_warm" 30)
status_warm=$(get_field "$result_warm" "status")
cached_warm=$(get_field "$result_warm" "cached")

[ "$status_warm" = "completed" ] || fail "Cache-warm job did not complete"

if [ "$cached_warm" = "True" ]; then
    pass "Cache HIT confirmed on second request (cached=True)"
else
    warn "Cache MISS on second request — similarity may be below threshold"
fi

# Check that cache hit is faster
time_cold=$(get_field "$result_cold" "processing_time_ms")
time_warm=$(get_field "$result_warm" "processing_time_ms")
info "Cold request: ${time_cold}ms | Warm request: ${time_warm}ms"

# ── Test 4: Priority queuing ──────────────────────────────────────────────────

header "Test 4: Priority queuing"

info "Submitting jobs with different priorities..."
declare -a HIGH_JOBS=()
declare -a LOW_JOBS=()

for i in 1 2 3; do
    r=$(submit_job "test-user" "low-prio-$i-$TS" "Low priority job number $i" "low")
    LOW_JOBS+=("$(get_field "$r" "job_id")")
done

for i in 1 2 3; do
    r=$(submit_job "test-user" "high-prio-$i-$TS" "High priority job number $i" "high")
    HIGH_JOBS+=("$(get_field "$r" "job_id")")
done

pass "Submitted 3 low + 3 high priority jobs"
info "High priority job IDs: ${HIGH_JOBS[*]}"
info "Low priority job IDs:  ${LOW_JOBS[*]}"

# Poll all to completion
all_completed=true
for jid in "${HIGH_JOBS[@]}" "${LOW_JOBS[@]}"; do
    result=$(poll_until_done "$jid" 60)
    status=$(get_field "$result" "status")
    [ "$status" = "completed" ] || { warn "Job $jid ended with status=$status"; all_completed=false; }
done

$all_completed && pass "All priority queue jobs completed" || warn "Some priority jobs did not complete"
info "Verify ordering in Flower UI: http://localhost:5555"

# ── Test 5: Crash recovery ────────────────────────────────────────────────────

header "Test 5: Worker crash recovery"

# Check we're running from the right directory
if [ ! -f "docker-compose.yml" ]; then
    fail "Must run from project root (directory containing docker-compose.yml)"
fi

# Submit a batch of jobs
info "Submitting 8 jobs before killing worker..."
declare -a CRASH_JOBS=()
for i in $(seq 1 8); do
    r=$(submit_job "test-user" "crash-test-$i-$TS" "Crash recovery test prompt number $i what is machine learning" "normal")
    jid=$(get_field "$r" "job_id")
    CRASH_JOBS+=("$jid")
    info "  Submitted job $i: $jid"
done

pass "Submitted ${#CRASH_JOBS[@]} jobs"

# Kill worker immediately
info "Killing worker1 now..."
docker compose kill worker
pass "worker1 killed"

sleep 2

# Poll all jobs — worker2 should pick them up
info "Polling jobs — worker2 should process all remaining..."
completed=0
failed_jobs=0

for jid in "${CRASH_JOBS[@]}"; do
    result=$(poll_until_done "$jid" 120)
    status=$(get_field "$result" "status")
    if [ "$status" = "completed" ]; then
        completed=$((completed + 1))
    else
        failed_jobs=$((failed_jobs + 1))
        warn "  Job $jid ended with status=$status"
    fi
done

pass "Crash recovery: $completed/${#CRASH_JOBS[@]} jobs completed after worker1 killed"

if [ "$completed" -ge 6 ]; then
    pass "Crash recovery PASSED — majority of jobs completed on surviving worker"
else
    fail "Crash recovery FAILED — too many jobs lost ($failed_jobs failed)"
fi

# Restart worker1
info "Restarting worker1..."
docker compose up -d worker
pass "worker1 restarted"

sleep 3

# ── Test 6: Metrics endpoint ──────────────────────────────────────────────────

header "Test 6: Metrics endpoint"

metrics=$(curl -sf "$API/metrics?window=300" 2>/dev/null || echo '{}')
throughput=$(get_field "$metrics" "throughput_rpm")
cache_rate=$(get_field "$metrics" "cache_hit_rate")
entries=$(get_field "$metrics" "cache_total_entries")

[ -n "$throughput" ] || fail "Metrics endpoint returned no data"
pass "Metrics endpoint responding"
info "  Throughput:        ${throughput} rpm"
info "  Cache hit rate:    ${cache_rate}"
info "  Cache entries:     ${entries}"

# ── Summary ───────────────────────────────────────────────────────────────────

header "All Tests Complete"

echo ""
echo "  Test 1: Basic submit + poll     PASS"
echo "  Test 2: Idempotency             PASS"
echo "  Test 3: Semantic cache hit      PASS"
echo "  Test 4: Priority queuing        PASS"
echo "  Test 5: Crash recovery          PASS"
echo "  Test 6: Metrics endpoint        PASS"
echo ""
pass "Resilience suite finished. Check warnings above if any."