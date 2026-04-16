from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.database import get_db
from src.core.logging import get_logger
from src.models.db import PromptRequest
from src.models.schemas import ProcessRequest, SubmitResponse, JobStatus
from src.workers.tasks import process_prompt

router = APIRouter()
logger = get_logger(__name__)
settings = get_settings()

PRIORITY_TO_QUEUE = {"high": "high", "normal": "normal", "low": "low"}


@router.post("/process", response_model=SubmitResponse, status_code=202)
async def submit_prompt(body: ProcessRequest, db: AsyncSession = Depends(get_db)):
    # Idempotency — same prompt_id returns the existing job
    existing = await db.execute(
        select(PromptRequest).where(PromptRequest.prompt_id == body.prompt_id)
    )
    existing_row = existing.scalar_one_or_none()

    if existing_row is not None:
        job_id = existing_row.task_id or existing_row.prompt_id
        return SubmitResponse(
            job_id=job_id,
            prompt_id=existing_row.prompt_id,
            user_id=existing_row.user_id,
            status=JobStatus(existing_row.status),
            poll_url=f"{settings.api_v1_prefix}/status/{job_id}",
        )

    record = PromptRequest(
        prompt_id=body.prompt_id,
        user_id=body.user_id,
        text=body.text,
        priority=body.priority.value,
        status="pending",
    )
    db.add(record)
    await db.flush()

    queue = PRIORITY_TO_QUEUE[body.priority.value]
    task = process_prompt.apply_async(
        kwargs={"prompt_id": body.prompt_id, "user_id": body.user_id, "text": body.text},
        queue=queue,
    )

    record.task_id = task.id
    await db.commit()

    logger.info("prompt_enqueued", prompt_id=body.prompt_id, task_id=task.id, queue=queue)

    return SubmitResponse(
        job_id=task.id,
        prompt_id=body.prompt_id,
        user_id=body.user_id,
        status=JobStatus.pending,
        poll_url=f"{settings.api_v1_prefix}/status/{task.id}",
    )