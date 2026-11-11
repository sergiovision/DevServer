"""Service configuration. Every limit is an environment variable (see README)."""

from __future__ import annotations

from datetime import timedelta
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://reservations:reservations@localhost:5440/reservations"
    db_pool_min_size: int = 2
    db_pool_max_size: int = 30

    # API (level 1)
    max_items_per_batch: int = 1000
    max_body_bytes: int = 5 * 1024 * 1024
    batches_per_minute_per_client: int = 10
    items_per_day_per_client: int = 20000

    # Chunking / worker (levels 2 and 3)
    chunk_size: int = 25
    worker_concurrency: int = 8
    # PgQueuer entrypoint rate cap per worker process; 0 means unlimited.
    worker_requests_per_second: float = 0
    client_max_concurrent_chunks: int = 2
    client_busy_delay_s: float = 2
    item_max_attempts: int = 3
    item_retry_base_s: float = 0.2
    # A job whose worker stops heartbeating for this long is picked up again
    # by another worker (PgQueuer ``retry_timer``).
    stuck_item_timeout_s: float = 300

    # Webhooks
    webhook_secret: str = "dev-webhook-secret"
    webhook_max_attempts: int = 5
    webhook_timeout_s: float = 5
    # Delay before attempt N+1 after attempt N failed (the last value repeats).
    webhook_backoff_s: str = "5,25,120,600,1800"

    log_level: str = "INFO"

    @property
    def client_busy_delay(self) -> timedelta:
        return timedelta(seconds=self.client_busy_delay_s)

    @property
    def job_retry_timer(self) -> timedelta:
        return timedelta(seconds=self.stuck_item_timeout_s)

    @property
    def webhook_backoff(self) -> list[float]:
        return [float(x) for x in self.webhook_backoff_s.split(",") if x.strip()]

    def webhook_delay_after(self, attempt: int) -> timedelta:
        schedule = self.webhook_backoff or [5.0]
        return timedelta(seconds=schedule[min(attempt - 1, len(schedule) - 1)])


@lru_cache
def get_settings() -> Settings:
    return Settings()
