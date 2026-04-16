from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Priority(str, Enum):
    high = "high"
    normal = "normal"
    low = "low"


class JobStatus(str, Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class ProcessRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_\-]+$")
    prompt_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_\-]+$")
    text: str = Field(..., min_length=1, max_length=32_000)
    priority: Priority = Priority.normal


class SubmitResponse(BaseModel):
    job_id: str
    prompt_id: str
    user_id: str
    status: JobStatus = JobStatus.pending
    poll_url: str


class ProcessResponse(BaseModel):
    user_id: str
    prompt_id: str
    status: JobStatus
    cached: bool = False
    response: str | None = None
    processing_time_ms: int | None = None
    retry_count: int = 0
    error: str | None = None


class HealthComponent(BaseModel):
    database: str
    worker: str
    cache: str


class HealthResponse(BaseModel):
    status: str
    timestamp: datetime
    components: HealthComponent
    version: str = "1.0.0"


class MetricsResponse(BaseModel):
    window_s: int
    throughput_rpm: float
    cache_hit_rate: float
    error_rate: float
    latency_ms: dict[str, float]
    rate_limit: dict[str, Any]
    cache_total_entries: int


class ErrorDetail(BaseModel):
    field: str
    msg: str


class ProblemDetail(BaseModel):
    type: str
    title: str
    status: int
    detail: list[ErrorDetail] | str | None = None
    retry_after: int | None = None