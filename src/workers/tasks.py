import time
from datetime import datetime, timezone

from celery import Task
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from src.core.config import get_settings
from src.core.logging import get_logger, setup_logging
from src.models.db import PromptRequest
from src.services.cache import CacheService
from src.services.llm import MockLLM, LLMProviderError, RateLimitExceeded, get_llm
from src.workers.celery_app import celery_app

setup_logging()
logger = get_logger(__name__)
settings = get_settings()

_sync_engine = create_engine(
    settings.database_url_sync,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
)
SyncSession = sessionmaker(bind=_sync_engine, expire_on_commit=False)


def _get_request(db: Session, prompt_id: str) -> PromptRequest | None:
    return db.query(PromptRequest).filter(PromptRequest.prompt_id == prompt_id).first()


def _call_llm_with_retry(llm: MockLLM, text: str, max_retries: int) -> tuple[str, int]:
    retry_count = 0
    last_error = None

    for attempt in range(max_retries + 1):
        try:
            return llm.complete(text), retry_count
        except RateLimitExceeded:
            raise
        except LLMProviderError as e:
            last_error = e
            retry_count += 1
            if attempt < max_retries:
                delay = settings.llm_retry_base_delay * (2 ** attempt)
                logger.warning("llm_retry", attempt=attempt + 1, delay_s=delay)
                time.sleep(delay)

    raise LLMProviderError(f"LLM failed after {max_retries} retries: {last_error}")


@celery_app.task(
    name="src.workers.tasks.process_prompt",
    bind=True,
    max_retries=0,
    track_started=True,
)
def process_prompt(self: Task, prompt_id: str, user_id: str, text: str) -> dict:
    start_time = time.monotonic()

    with SyncSession() as db:
        request = _get_request(db, prompt_id)
        if request is None:
            return {"status": "failed", "error": "Request record not found"}

        # Idempotency — already done, return stored result immediately
        if request.status == "completed":
            return {
                "status": "completed",
                "response": request.response,
                "cached": request.cached,
                "retry_count": request.retry_count,
                "processing_time_ms": request.processing_time_ms,
            }

        request.status = "processing"
        request.task_id = self.request.id
        db.commit()

        try:
            # Cache lookup
            cache = CacheService(db)
            cache_result = cache.lookup(text)

            if cache_result is not None:
                cached_response, similarity = cache_result
                elapsed_ms = round((time.monotonic() - start_time) * 1000)
                request.status = "completed"
                request.response = cached_response
                request.cached = True
                request.processing_time_ms = elapsed_ms
                db.commit()
                return {
                    "status": "completed",
                    "response": cached_response,
                    "cached": True,
                    "retry_count": 0,
                    "processing_time_ms": elapsed_ms,
                }

            # Rate limit + LLM call
            llm = get_llm()
            while True:
                try:
                    response, retry_count = _call_llm_with_retry(llm, text, settings.llm_max_retries)
                    break
                except RateLimitExceeded as e:
                    logger.info("waiting_for_rate_limit_slot", wait_s=e.retry_after)
                    time.sleep(e.retry_after)

            # Store + complete
            cache.store(text, response)
            elapsed_ms = round((time.monotonic() - start_time) * 1000)
            request.status = "completed"
            request.response = response
            request.cached = False
            request.retry_count = retry_count
            request.processing_time_ms = elapsed_ms
            db.commit()

            return {
                "status": "completed",
                "response": response,
                "cached": False,
                "retry_count": retry_count,
                "processing_time_ms": elapsed_ms,
            }

        except LLMProviderError as e:
            elapsed_ms = round((time.monotonic() - start_time) * 1000)
            request.status = "failed"
            request.error = str(e)
            request.retry_count = settings.llm_max_retries
            request.processing_time_ms = elapsed_ms
            db.commit()
            return {"status": "failed", "error": str(e), "retry_count": settings.llm_max_retries}

        except Exception as e:
            elapsed_ms = round((time.monotonic() - start_time) * 1000)
            request.status = "failed"
            request.error = f"{type(e).__name__}: {e}"
            request.processing_time_ms = elapsed_ms
            db.commit()
            return {"status": "failed", "error": f"{type(e).__name__}: {e}", "retry_count": 0}