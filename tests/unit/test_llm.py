import time
from unittest.mock import MagicMock, patch

import pytest

from src.services.llm import MockLLM, RateLimitExceeded, LLMProviderError


@pytest.fixture
def mock_redis():
    with patch("src.services.llm.redis_sync.from_url") as mock_from_url:
        client = MagicMock()
        mock_from_url.return_value = client
        script = MagicMock(return_value=[1, 0])
        client.register_script.return_value = script
        yield client, script


@pytest.fixture
def llm(mock_redis):
    return MockLLM()


class TestRateLimiting:
    def test_allowed_when_under_limit(self, llm, mock_redis):
        _, script = mock_redis
        script.return_value = [1, 0]
        with patch("time.sleep"):
            result = llm.complete("test prompt")
        assert result is not None

    def test_raises_when_rate_limited(self, llm, mock_redis):
        _, script = mock_redis
        script.return_value = [0, 12]
        with pytest.raises(RateLimitExceeded) as exc_info:
            llm.complete("test prompt")
        assert exc_info.value.retry_after == 12

    def test_lua_script_called_with_correct_key(self, llm, mock_redis):
        _, script = mock_redis
        script.return_value = [1, 0]
        with patch("time.sleep"):
            llm.complete("test")
        call_args = script.call_args
        keys = call_args.kwargs.get("keys") or call_args.args[0]
        assert keys == ["llm:rate_limit:sliding_window"]

    def test_lua_script_called_with_four_args(self, llm, mock_redis):
        _, script = mock_redis
        script.return_value = [1, 0]
        with patch("time.sleep"):
            llm.complete("test")
        call_args = script.call_args
        args = call_args.kwargs.get("args") or call_args.args[1]
        assert len(args) == 4

    def test_window_ms_is_60000(self, llm, mock_redis):
        _, script = mock_redis
        script.return_value = [1, 0]
        with patch("time.sleep"):
            llm.complete("test")
        call_args = script.call_args
        args = call_args.kwargs.get("args") or call_args.args[1]
        assert int(args[1]) == 60_000


class TestFailureSimulation:
    def test_approximate_5_percent_failure_rate(self, llm, mock_redis):
        _, script = mock_redis
        script.return_value = [1, 0]
        failures = 0
        with patch("time.sleep"):
            for _ in range(1000):
                try:
                    llm.complete("test prompt")
                except LLMProviderError:
                    failures += 1
        rate = failures / 1000
        assert 0.02 <= rate <= 0.08, f"Expected ~5% failure rate, got {rate:.2%}"

    def test_zero_failure_rate_never_fails(self, llm, mock_redis):
        _, script = mock_redis
        script.return_value = [1, 0]
        with patch.object(llm, "_failure_rate", 0.0), patch("time.sleep"):
            for _ in range(50):
                result = llm.complete("test")
                assert result is not None

    def test_full_failure_rate_always_fails(self, llm, mock_redis):
        _, script = mock_redis
        script.return_value = [1, 0]
        with patch.object(llm, "_failure_rate", 1.0), patch("time.sleep"):
            with pytest.raises(LLMProviderError):
                llm.complete("test")


class TestResponseGeneration:
    def test_same_prompt_same_response(self, llm):
        r1 = llm._generate_response("explain quantum computing")
        r2 = llm._generate_response("explain quantum computing")
        assert r1 == r2

    def test_different_prompts_different_responses(self, llm):
        r1 = llm._generate_response("prompt A")
        r2 = llm._generate_response("prompt B")
        assert r1 != r2

    def test_response_contains_prompt_text(self, llm):
        prompt = "What is machine learning?"
        response = llm._generate_response(prompt)
        assert "What is machine learning?" in response