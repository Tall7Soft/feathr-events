"""Application settings.

12-factor configuration via environment variables. Every value the
service reads at runtime — service URLs, worker tuning, retry budgets,
TTLs, rate limits — lives here and is validated at startup. Defaults
target a single-replica deploy talking to local Docker; production
deployments override via ``.env`` or the container environment.

See ``.env.example`` for the canonical knob list and
``ARCHITECTURE.md`` §10 for the SLOs these knobs back.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed view of every environment-variable-driven knob.

    Values are validated on instantiation; an out-of-range
    ``WORKER_CONCURRENCY`` or a non-numeric ``RATE_LIMIT_PER_MINUTE``
    fails the app at startup rather than silently mis-behaving in
    production.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = "events"
    mongo_server_selection_timeout_ms: int = Field(default=3000, ge=500, le=60_000)
    # socketTimeoutMS bounds how long an in-flight socket read can wait
    # before the worker gives up and triggers retry/DLQ. Without it, a
    # frozen-but-reachable server would block the worker forever.
    mongo_socket_timeout_ms: int = Field(default=10_000, ge=1000, le=120_000)

    elasticsearch_url: str = "http://localhost:9200"
    elasticsearch_index: str = "events"

    redis_url: str = "redis://localhost:6379/0"

    worker_concurrency: int = Field(default=4, ge=1, le=64)
    worker_batch_size: int = Field(default=50, ge=1, le=1000)
    worker_batch_timeout_seconds: float = Field(default=1.0, gt=0)
    queue_max_receive_count: int = Field(default=3, ge=1, le=20)
    queue_base_retry_delay_seconds: float = Field(default=1.0, gt=0)

    realtime_cache_ttl_seconds: int = Field(default=10, ge=1, le=3600)
    realtime_window_seconds: int = Field(default=300, ge=10, le=86400)

    rate_limit_per_minute: int = Field(default=600, ge=1)
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide cached :class:`Settings` instance.

    Wrapped in ``lru_cache`` so we instantiate (and re-validate) the
    settings exactly once per process. FastAPI dependency providers
    and the lifespan both call this; both see the same object.
    """
    return Settings()
