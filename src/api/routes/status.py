from celery.result import AsyncResult
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.models.db import PromptRequest
from src.models.schemas import ProcessResponse, JobStatus
from src.workers.celery_app import celery_app

router = APIRouter()


@router.get("/status/{job_id}", response_model=ProcessResponse)
async def get_job_status(job_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(PromptRequest).where(PromptRequest.task_id == job_id)
    )
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    if record.status in ("completed", "failed"):
        return ProcessResponse(
            user_id=record.user_id,
            prompt_id=record.prompt_id,
            status=JobStatus(record.status),
            cached=record.cached,
            response=record.response,
            processing_time_ms=record.processing_time_ms,
            retry_count=record.retry_count or 0,
            error=record.error,
        )

    # Still in flight — check Celery backend too
    celery_result = AsyncResult(job_id, app=celery_app)
    if celery_result.ready():
        data = celery_result.result or {}
        return ProcessResponse(
            user_id=record.user_id,
            prompt_id=record.prompt_id,
            status=JobStatus(data.get("status", record.status)),
            cached=data.get("cached", False),
            response=data.get("response"),
            processing_time_ms=data.get("processing_time_ms"),
            retry_count=data.get("retry_count", 0),
            error=data.get("error"),
        )

    return ProcessResponse(
        user_id=record.user_id,
        prompt_id=record.prompt_id,
        status=JobStatus(record.status),
    )