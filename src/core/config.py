from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://promptuser:promptpass@localhost:5432/promptdb"
    database_url_sync: str = "postgresql+psycopg2://promptuser:promptpass@localhost:5432/promptdb"

    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    rate_limit_max_calls: int = 300
    rate_limit_window_seconds: int = 60

    cache_similarity_threshold: float = 0.9
    embedding_model: str = "all-MiniLM-L6-v2"
    use_mock_embeddings: bool = False

    llm_latency_min_ms: int = 200
    llm_latency_max_ms: int = 500
    llm_failure_rate: float = 0.05
    llm_max_retries: int = 3
    llm_retry_base_delay: float = 1.0

    api_v1_prefix: str = "/v1"

    log_level: str = "INFO"
    log_format: str = "json"


@lru_cache
def get_settings() -> Settings:
    return Settings()