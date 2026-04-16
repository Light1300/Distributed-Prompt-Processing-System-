from celery import Celery
from src.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "prompt_processor",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["src.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    result_expires=3600,
    timezone="UTC",
    enable_utc=True,
    task_queues={
        "high":   {"exchange": "high",   "routing_key": "high"},
        "normal": {"exchange": "normal", "routing_key": "normal"},
        "low":    {"exchange": "low",    "routing_key": "low"},
    },
    task_default_queue="normal",
    task_default_exchange="normal",
    task_default_routing_key="normal",
    worker_prefetch_multiplier=1,
    task_track_started=True,
)