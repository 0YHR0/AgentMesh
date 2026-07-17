from functools import lru_cache

from pydantic import AliasChoices, Field, NonNegativeInt, PositiveInt, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing_extensions import Self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTMESH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    environment: str = "development"
    database_url: str = "postgresql+psycopg://agentmesh:agentmesh@127.0.0.1:5432/agentmesh"
    checkpoint_database_url: str = "postgresql://agentmesh:agentmesh@127.0.0.1:5432/agentmesh"
    redis_url: str = "redis://127.0.0.1:6379/0"
    tenant_id: str = "default"
    agent_id: str = "demo-agent"
    execution_stream: str = "agentmesh.run-requests"
    domain_event_stream: str = "agentmesh.domain-events"
    execution_group: str = "agentmesh-run-workers"
    execution_consumer_name: str = "run-executor-v1"
    dead_letter_stream: str = "agentmesh.dead-letter"
    worker_block_ms: NonNegativeInt = 1_000
    worker_pending_idle_ms: PositiveInt = 60_000
    run_lease_seconds: PositiveInt = 300
    run_lease_renewal_seconds: PositiveInt | None = None
    relay_batch_size: PositiveInt = 100
    relay_claim_seconds: PositiveInt = 30
    relay_retry_seconds: NonNegativeInt = 5
    retention_interval_seconds: PositiveInt = 300
    retention_batch_size: int = Field(default=1_000, ge=1, le=10_000)
    outbox_retention_seconds: PositiveInt = 604_800
    inbox_retention_seconds: PositiveInt = 2_592_000
    redis_stream_retention_seconds: PositiveInt = 604_800
    redis_stream_max_entries: PositiveInt = 100_000
    dead_letter_stream_retention_seconds: PositiveInt = 2_592_000
    dead_letter_stream_max_entries: PositiveInt = 50_000
    relay_metrics_enabled: bool = True
    relay_metrics_host: str = "0.0.0.0"
    relay_metrics_port: int = Field(default=9_464, ge=1, le=65_535)
    langfuse_enabled: bool = False
    langfuse_public_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("AGENTMESH_LANGFUSE_PUBLIC_KEY", "LANGFUSE_PUBLIC_KEY"),
    )
    langfuse_secret_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("AGENTMESH_LANGFUSE_SECRET_KEY", "LANGFUSE_SECRET_KEY"),
    )
    langfuse_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("AGENTMESH_LANGFUSE_BASE_URL", "LANGFUSE_BASE_URL"),
    )
    langfuse_timeout_seconds: int = Field(default=5, ge=1, le=60)
    feature_profile: str = "minimal"
    feature_gates: str = ""
    artifact_owner_id: str = "local-user"
    artifact_max_inline_bytes: PositiveInt = 65_536
    mcp_workspace_root: str = "."
    mcp_workspace_timeout_seconds: PositiveInt = 30
    mcp_workspace_max_bytes: PositiveInt = 65_536
    mcp_max_result_bytes: PositiveInt = 262_144

    @model_validator(mode="after")
    def validate_messaging_retention_horizons(self) -> Self:
        stream_names = {
            self.execution_stream,
            self.domain_event_stream,
            self.dead_letter_stream,
        }
        if len(stream_names) != 3:
            raise ValueError(
                "execution_stream, domain_event_stream, and dead_letter_stream "
                "must be distinct"
            )
        if self.outbox_retention_seconds < self.redis_stream_retention_seconds:
            raise ValueError(
                "outbox_retention_seconds must be greater than or equal to "
                "redis_stream_retention_seconds"
            )
        if self.inbox_retention_seconds < self.outbox_retention_seconds:
            raise ValueError(
                "inbox_retention_seconds must be greater than or equal to "
                "outbox_retention_seconds"
            )
        if self.inbox_retention_seconds < self.dead_letter_stream_retention_seconds:
            raise ValueError(
                "inbox_retention_seconds must be greater than or equal to "
                "dead_letter_stream_retention_seconds"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
