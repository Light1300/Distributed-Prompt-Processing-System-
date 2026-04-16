import asyncio
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.logging import get_logger
from src.models.db import PromptRequest, SemanticCache
from src.models.schemas import MetricsResponse
from src.services.llm import get_llm

router = APIRouter()
logger = get_logger(__name__)


@router.get("/metrics", response_model=MetricsResponse)
async def get_metrics(
    window: int = Query(default=60, ge=10, le=3600),
    db: AsyncSession = Depends(get_db),
):
    since = datetime.now(timezone.utc) - timedelta(seconds=window)

    total = (await db.execute(
        select(func.count()).select_from(PromptRequest)
        .where(PromptRequest.created_at >= since)
    )).scalar() or 0

    completed = (await db.execute(
        select(func.count()).select_from(PromptRequest)
        .where(and_(PromptRequest.created_at >= since, PromptRequest.status == "completed"))
    )).scalar() or 0

    cache_hits = (await db.execute(
        select(func.count()).select_from(PromptRequest)
        .where(and_(
            PromptRequest.created_at >= since,
            PromptRequest.status == "completed",
            PromptRequest.cached == True,  # noqa: E712
        ))
    )).scalar() or 0

    failed = (await db.execute(
        select(func.count()).select_from(PromptRequest)
        .where(and_(PromptRequest.created_at >= since, PromptRequest.status == "failed"))
    )).scalar() or 0

    latency_rows = (await db.execute(
        select(PromptRequest.processing_time_ms)
        .where(and_(
            PromptRequest.created_at >= since,
            PromptRequest.status == "completed",
            PromptRequest.processing_time_ms.isnot(None),
        ))
        .order_by(PromptRequest.processing_time_ms)
    )).fetchall()
    latencies = [r[0] for r in latency_rows]

    def pct(data, p):
        if not data:
            return 0.0
        return float(data[min(int(len(data) * p / 100), len(data) - 1)])

    cache_total = (await db.execute(
        select(func.count()).select_from(SemanticCache)
    )).scalar() or 0

    loop = asyncio.get_event_loop()
    rate_limit_status = await loop.run_in_executor(None, get_llm().get_rate_limit_status)

    return MetricsResponse(
        window_s=window,
        throughput_rpm=round((total / window) * 60, 2) if total else 0.0,
        cache_hit_rate=round(cache_hits / completed, 4) if completed else 0.0,
        error_rate=round(failed / total, 4) if total else 0.0,
        latency_ms={"p50": pct(latencies, 50), "p95": pct(latencies, 95), "p99": pct(latencies, 99)},
        rate_limit=rate_limit_status,
        cache_total_entries=cache_total,
    )