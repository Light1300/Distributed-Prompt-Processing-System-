
import uuid
import os
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase

# Set env before any src imports
os.environ.update({
    "DATABASE_URL": "sqlite+aiosqlite:///./test_integration.db",
    "DATABASE_URL_SYNC": "sqlite:///./test_integration.db",
    "REDIS_URL": "redis://localhost:6379/0",
    "CELERY_BROKER_URL": "redis://localhost:6379/0",
    "CELERY_RESULT_BACKEND": "redis://localhost:6379/1",
    "USE_MOCK_EMBEDDINGS": "true",
    "LOG_FORMAT": "console",
    "LOG_LEVEL": "WARNING",
})

from src.core.database import Base
from src.models.db import PromptRequest, SemanticCache
from sqlalchemy.pool import NullPool


# Test database setup 

TEST_DB_URL = "sqlite+aiosqlite:///./test_integration.db"

test_engine = create_async_engine(
    TEST_DB_URL,
    echo=False,
    poolclass=NullPool,
)

TestSessionLocal = async_sessionmaker(
    bind=test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def override_get_db():
    async with TestSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@pytest.fixture(scope="session", autouse=True)
async def setup_test_db():
    """Create all tables once for the test session."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await test_engine.dispose()
    # Clean up db file
    if os.path.exists("./test_integration.db"):
        os.remove("./test_integration.db")


@pytest.fixture(autouse=True)
async def clean_tables():
    """Wipe tables between tests for isolation."""
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(lambda c: c.execute(
            SemanticCache.__table__.delete()
        ))
        await conn.run_sync(lambda c: c.execute(
            PromptRequest.__table__.delete()
        ))


@pytest.fixture
async def client():
    """AsyncClient wired to the FastAPI app with test DB injected."""
    from src.api.main import app
    from src.core.database import get_db

    app.dependency_overrides[get_db] = override_get_db

    # Patch lifespan to skip model loading and create_all
    async def mock_lifespan(app):
        yield

    with patch("src.api.main.engine", test_engine), \
         patch("src.services.embeddings.get_embedder"):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            yield ac

    app.dependency_overrides.clear()


def make_request(prompt_id: str = None, text: str = "Test prompt", priority: str = "normal") -> dict:
    return {
        "user_id": "test-user",
        "prompt_id": prompt_id or f"p-{uuid.uuid4().hex[:8]}",
        "text": text,
        "priority": priority,
    }


#  Validation tests

class TestValidation:
    async def test_missing_user_id_returns_422(self, client):
        resp = await client.post("/v1/process", json={
            "prompt_id": "p1", "text": "hello"
        })
        assert resp.status_code == 422

    async def test_missing_prompt_id_returns_422(self, client):
        resp = await client.post("/v1/process", json={
            "user_id": "u1", "text": "hello"
        })
        assert resp.status_code == 422

    async def test_empty_text_returns_422(self, client):
        resp = await client.post("/v1/process", json={
            "user_id": "u1", "prompt_id": "p1", "text": ""
        })
        assert resp.status_code == 422

    async def test_invalid_priority_returns_422(self, client):
        resp = await client.post("/v1/process", json={
            "user_id": "u1", "prompt_id": "p1",
            "text": "hello", "priority": "urgent"
        })
        assert resp.status_code == 422

    async def test_invalid_user_id_chars_returns_422(self, client):
        resp = await client.post("/v1/process", json={
            "user_id": "u1 with spaces", "prompt_id": "p1", "text": "hello"
        })
        assert resp.status_code == 422

    async def test_valid_request_accepted(self, client):
        mock_task = MagicMock()
        mock_task.id = str(uuid.uuid4())

        with patch("src.api.routes.process.process_prompt") as mock_proc:
            mock_proc.apply_async.return_value = mock_task
            resp = await client.post("/v1/process", json=make_request())

        assert resp.status_code == 202
        data = resp.json()
        assert "job_id" in data
        assert "poll_url" in data
        assert data["status"] == "pending"

    async def test_default_priority_is_normal(self, client):
        mock_task = MagicMock()
        mock_task.id = str(uuid.uuid4())

        with patch("src.api.routes.process.process_prompt") as mock_proc:
            mock_proc.apply_async.return_value = mock_task
            resp = await client.post("/v1/process", json={
                "user_id": "u1", "prompt_id": f"p-{uuid.uuid4().hex[:6]}", "text": "hello"
            })

        assert resp.status_code == 202

    @pytest.mark.parametrize("priority", ["high", "normal", "low"])
    async def test_all_priorities_accepted(self, client, priority):
        mock_task = MagicMock()
        mock_task.id = str(uuid.uuid4())

        with patch("src.api.routes.process.process_prompt") as mock_proc:
            mock_proc.apply_async.return_value = mock_task
            resp = await client.post("/v1/process", json=make_request(priority=priority))

        assert resp.status_code == 202


#  Idempotency tests 

class TestIdempotency:
    async def test_same_prompt_id_returns_same_job(self, client):
        mock_task = MagicMock()
        mock_task.id = str(uuid.uuid4())
        pid = f"idem-{uuid.uuid4().hex[:8]}"

        with patch("src.api.routes.process.process_prompt") as mock_proc:
            mock_proc.apply_async.return_value = mock_task

            r1 = await client.post("/v1/process", json=make_request(prompt_id=pid))
            r2 = await client.post("/v1/process", json=make_request(prompt_id=pid))

        assert r1.status_code == 202
        assert r2.status_code == 202
        assert r1.json()["job_id"] == r2.json()["job_id"]

    async def test_task_enqueued_only_once_for_same_prompt_id(self, client):
        mock_task = MagicMock()
        mock_task.id = str(uuid.uuid4())
        pid = f"idem-once-{uuid.uuid4().hex[:8]}"

        with patch("src.api.routes.process.process_prompt") as mock_proc:
            mock_proc.apply_async.return_value = mock_task

            await client.post("/v1/process", json=make_request(prompt_id=pid))
            await client.post("/v1/process", json=make_request(prompt_id=pid))
            await client.post("/v1/process", json=make_request(prompt_id=pid))

        # apply_async should only be called once — subsequent calls hit idempotency check
        assert mock_proc.apply_async.call_count == 1


#  Status polling tests 

class TestStatusPolling:
    async def test_unknown_job_id_returns_404(self, client):
        resp = await client.get(f"/v1/status/{uuid.uuid4()}")
        assert resp.status_code == 404

    async def test_pending_job_returns_pending_status(self, client):
        mock_task = MagicMock()
        mock_task.id = str(uuid.uuid4())

        with patch("src.api.routes.process.process_prompt") as mock_proc, \
             patch("src.api.routes.status.AsyncResult") as mock_result:
            mock_proc.apply_async.return_value = mock_task
            mock_result.return_value.ready.return_value = False

            await client.post("/v1/process", json=make_request())
            resp = await client.get(f"/v1/status/{mock_task.id}")

        assert resp.status_code == 200
        assert resp.json()["status"] in ("pending", "processing")

    async def test_completed_job_returns_full_response(self, client):
        """Simulate a job that completed — write directly to DB and poll."""
        task_id = str(uuid.uuid4())
        pid = f"completed-{uuid.uuid4().hex[:8]}"

        # Write a completed record directly
        async with TestSessionLocal() as db:
            record = PromptRequest(
                prompt_id=pid,
                user_id="test-user",
                text="test prompt",
                priority="normal",
                status="completed",
                response="This is the LLM response",
                cached=False,
                retry_count=0,
                processing_time_ms=350,
                task_id=task_id,
            )
            db.add(record)
            await db.commit()

        resp = await client.get(f"/v1/status/{task_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert data["response"] == "This is the LLM response"
        assert data["processing_time_ms"] == 350
        assert data["cached"] is False

    async def test_failed_job_returns_error_fields(self, client):
        task_id = str(uuid.uuid4())
        pid = f"failed-{uuid.uuid4().hex[:8]}"

        async with TestSessionLocal() as db:
            record = PromptRequest(
                prompt_id=pid,
                user_id="test-user",
                text="test prompt",
                priority="normal",
                status="failed",
                error="LLM provider timeout after 3 retries",
                retry_count=3,
                task_id=task_id,
            )
            db.add(record)
            await db.commit()

        resp = await client.get(f"/v1/status/{task_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "failed"
        assert data["error"] == "LLM provider timeout after 3 retries"
        assert data["retry_count"] == 3

    async def test_cached_job_returns_cached_true(self, client):
        task_id = str(uuid.uuid4())
        pid = f"cached-{uuid.uuid4().hex[:8]}"

        async with TestSessionLocal() as db:
            record = PromptRequest(
                prompt_id=pid,
                user_id="test-user",
                text="explain quantum computing",
                priority="high",
                status="completed",
                response="Quantum response from cache",
                cached=True,
                retry_count=0,
                processing_time_ms=45,
                task_id=task_id,
            )
            db.add(record)
            await db.commit()

        resp = await client.get(f"/v1/status/{task_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cached"] is True
        assert data["processing_time_ms"] == 45


#  Priority routing tests

class TestPriorityRouting:
    @pytest.mark.parametrize("priority,expected_queue", [
        ("high", "high"),
        ("normal", "normal"),
        ("low", "low"),
    ])
    async def test_priority_routes_to_correct_queue(self, client, priority, expected_queue):
        mock_task = MagicMock()
        mock_task.id = str(uuid.uuid4())

        with patch("src.api.routes.process.process_prompt") as mock_proc:
            mock_proc.apply_async.return_value = mock_task
            await client.post("/v1/process", json=make_request(priority=priority))

        call_kwargs = mock_proc.apply_async.call_args.kwargs
        assert call_kwargs["queue"] == expected_queue, (
            f"Priority '{priority}' should route to queue '{expected_queue}', "
            f"got '{call_kwargs['queue']}'"
        )


#  Health endpoint tests 

class TestHealthEndpoint:
    async def test_health_returns_200_when_db_up(self, client):
        with patch("src.api.routes.health._check_database", return_value="connected"), \
             patch("src.api.routes.health._check_redis", return_value="available"), \
             patch("src.api.routes.health._check_worker", return_value="running"):
            resp = await client.get("/v1/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["components"]["database"] == "connected"
        assert data["components"]["cache"] == "available"
        assert data["components"]["worker"] == "running"
        assert "timestamp" in data
        assert "version" in data

    async def test_health_returns_503_when_db_down(self, client):
        with patch("src.api.routes.health._check_database", return_value="unreachable"), \
             patch("src.api.routes.health._check_redis", return_value="available"), \
             patch("src.api.routes.health._check_worker", return_value="running"):
            resp = await client.get("/v1/health")

        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "degraded"

    async def test_health_returns_503_when_redis_down(self, client):
        with patch("src.api.routes.health._check_database", return_value="connected"), \
             patch("src.api.routes.health._check_redis", return_value="unreachable"), \
             patch("src.api.routes.health._check_worker", return_value="running"):
            resp = await client.get("/v1/health")

        assert resp.status_code == 503

    async def test_health_worker_down_still_degraded(self, client):
        with patch("src.api.routes.health._check_database", return_value="connected"), \
             patch("src.api.routes.health._check_redis", return_value="available"), \
             patch("src.api.routes.health._check_worker", return_value="unreachable"):
            resp = await client.get("/v1/health")

        # Worker down alone should not make it 503 — DB and Redis are critical
        assert resp.status_code == 200


#  Metrics endpoint tests 

class TestMetricsEndpoint:
    async def test_metrics_returns_correct_shape(self, client):
        with patch("src.api.routes.metrics.get_llm") as mock_llm:
            mock_llm.return_value.get_rate_limit_status.return_value = {
                "calls_used": 42,
                "calls_remaining": 258,
                "window_reset_in": 60,
            }
            resp = await client.get("/v1/metrics")

        assert resp.status_code == 200
        data = resp.json()
        assert "window_s" in data
        assert "throughput_rpm" in data
        assert "cache_hit_rate" in data
        assert "error_rate" in data
        assert "latency_ms" in data
        assert "p50" in data["latency_ms"]
        assert "p95" in data["latency_ms"]
        assert "p99" in data["latency_ms"]
        assert "rate_limit" in data
        assert "cache_total_entries" in data

    async def test_metrics_with_completed_requests(self, client):
        """Seed DB with known data and verify metrics calculations."""
        task_ids = [str(uuid.uuid4()) for _ in range(4)]
        pids = [f"metrics-{uuid.uuid4().hex[:6]}" for _ in range(4)]

        async with TestSessionLocal() as db:
            records = [
                PromptRequest(prompt_id=pids[0], user_id="u1", text="t1",
                              priority="normal", status="completed",
                              cached=True, processing_time_ms=50, task_id=task_ids[0]),
                PromptRequest(prompt_id=pids[1], user_id="u1", text="t2",
                              priority="normal", status="completed",
                              cached=False, processing_time_ms=400, task_id=task_ids[1]),
                PromptRequest(prompt_id=pids[2], user_id="u1", text="t3",
                              priority="normal", status="completed",
                              cached=True, processing_time_ms=30, task_id=task_ids[2]),
                PromptRequest(prompt_id=pids[3], user_id="u1", text="t4",
                              priority="normal", status="failed",
                              task_id=task_ids[3]),
            ]
            for r in records:
                db.add(r)
            await db.commit()

        with patch("src.api.routes.metrics.get_llm") as mock_llm:
            mock_llm.return_value.get_rate_limit_status.return_value = {
                "calls_used": 0, "calls_remaining": 300, "window_reset_in": 60
            }
            resp = await client.get("/v1/metrics?window=3600")

        assert resp.status_code == 200
        data = resp.json()

        # 3 completed, 1 failed out of 4 total
        assert data["error_rate"] == pytest.approx(0.25, abs=0.01)
        # 2 cache hits out of 3 completed
        assert data["cache_hit_rate"] == pytest.approx(0.6667, abs=0.01)

    async def test_metrics_window_parameter(self, client):
        with patch("src.api.routes.metrics.get_llm") as mock_llm:
            mock_llm.return_value.get_rate_limit_status.return_value = {
                "calls_used": 0, "calls_remaining": 300, "window_reset_in": 60
            }
            resp = await client.get("/v1/metrics?window=300")

        assert resp.status_code == 200
        assert resp.json()["window_s"] == 300

    async def test_metrics_invalid_window_returns_422(self, client):
        resp = await client.get("/v1/metrics?window=5")  # below min of 10
        assert resp.status_code == 422


#  Error handling tests 

class TestErrorHandling:
    async def test_404_on_unknown_route(self, client):
        resp = await client.get("/v1/nonexistent")
        assert resp.status_code == 404

    async def test_validation_error_response_shape(self, client):
        resp = await client.post("/v1/process", json={"bad": "data"})
        assert resp.status_code == 422
        data = resp.json()
        assert "title" in data
        assert "status" in data
        assert data["status"] == 422