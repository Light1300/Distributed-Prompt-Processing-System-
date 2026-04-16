import asyncio
from datetime import datetime, timezone

import redis.asyncio as aioredis
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from src.core.config import get_settings
from src.core.database import AsyncSessionLocal
from src.core.logging import get_logger
from src.models.schemas import HealthResponse, HealthComponent
from src.workers.celery_app import celery_app

router = APIRouter()
logger = get_logger(__name__)
settings = get_settings()


async def _check_database() -> str:
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        return "connected"
    except Exception as e:
        logger.warning("health_db_fail", error=str(e))
        return "unreachable"


async def _check_redis() -> str:
    try:
        client = aioredis.from_url(settings.redis_url, socket_connect_timeout=2)
        await client.ping()
        await client.aclose()
        return "available"
    except Exception as e:
        logger.warning("health_redis_fail", error=str(e))
        return "unreachable"


async def _check_worker() -> str:
    try:
        loop = asyncio.get_event_loop()
        responses = await loop.run_in_executor(
            None, lambda: celery_app.control.ping(timeout=2.0)
        )
        return "running" if responses else "unreachable"
    except Exception as e:
        logger.warning("health_worker_fail", error=str(e))
        return "unreachable"


@router.get("/health", response_model=HealthResponse)
async def health_check():
    db_status, redis_status, worker_status = await asyncio.gather(
        _check_database(), _check_redis(), _check_worker()
    )
    is_healthy = db_status == "connected" and redis_status == "available"
    response = HealthResponse(
        status="healthy" if is_healthy else "degraded",
        timestamp=datetime.now(timezone.utc),
        components=HealthComponent(
            database=db_status,
            worker=worker_status,
            cache=redis_status,
        ),
    )
    return JSONResponse(
        content=response.model_dump(mode="json"),
        status_code=200 if is_healthy else 503,
    )