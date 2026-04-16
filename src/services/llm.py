import hashlib
import random
import time

import redis as redis_sync
from src.core.config import get_settings
from src.core.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()

_RATE_LIMIT_LUA = """
local key    = KEYS[1]
local now    = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit  = tonumber(ARGV[3])
local jitter = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local count = redis.call('ZCARD', key)

if count >= limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local oldest_ts = oldest[2] and tonumber(oldest[2]) or now
    local retry_after = math.ceil((oldest_ts + window - now) / 1000)
    return {0, retry_after}
end

redis.call('ZADD', key, now, now .. '-' .. jitter)
redis.call('EXPIRE', key, math.ceil(window / 1000) + 5)
return {1, 0}
"""

_RATE_LIMIT_KEY = "llm:rate_limit:sliding_window"


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"Rate limit exceeded. Retry after {retry_after}s")


class LLMProviderError(Exception):
    pass


class MockLLM:
    def __init__(self) -> None:
        self._redis = redis_sync.from_url(
            settings.celery_broker_url,
            decode_responses=True,
            socket_connect_timeout=5,
        )
        self._script = self._redis.register_script(_RATE_LIMIT_LUA)
        self._max_calls = settings.rate_limit_max_calls
        self._window_ms = settings.rate_limit_window_seconds * 1000
        self._failure_rate = settings.llm_failure_rate
        self._latency_min = settings.llm_latency_min_ms / 1000
        self._latency_max = settings.llm_latency_max_ms / 1000

    def _check_rate_limit(self) -> None:
        now_ms = int(time.time() * 1000)
        jitter = random.randint(0, 999_999)
        result = self._script(
            keys=[_RATE_LIMIT_KEY],
            args=[now_ms, self._window_ms, self._max_calls, jitter],
        )
        allowed, retry_after = int(result[0]), int(result[1])
        if not allowed:
            raise RateLimitExceeded(retry_after=retry_after)

    def complete(self, prompt: str) -> str:
        self._check_rate_limit()
        latency = random.uniform(self._latency_min, self._latency_max)
        time.sleep(latency)
        if random.random() < self._failure_rate:
            raise LLMProviderError("Simulated LLM provider error (transient)")
        return self._generate_response(prompt)

    @staticmethod
    def _generate_response(prompt: str) -> str:
        digest = hashlib.sha256(prompt.encode()).hexdigest()[:8]
        return (
            f"[MockLLM hash={digest}] "
            f"Simulated response to: '{prompt[:80]}{'...' if len(prompt) > 80 else ''}'"
        )

    def get_rate_limit_status(self) -> dict:
        now_ms = int(time.time() * 1000)
        self._redis.zremrangebyscore(_RATE_LIMIT_KEY, "-inf", now_ms - self._window_ms)
        used = self._redis.zcard(_RATE_LIMIT_KEY)
        return {
            "calls_used": used,
            "calls_remaining": max(0, self._max_calls - used),
            "window_reset_in": self._window_ms // 1000,
        }


_llm_instance = None


def get_llm() -> MockLLM:
    global _llm_instance
    if _llm_instance is None:
        _llm_instance = MockLLM()
    return _llm_instance