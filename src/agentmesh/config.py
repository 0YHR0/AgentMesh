from functools import lru_cache

from pydantic import AliasChoices, Field, NonNegativeInt, PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    relay_batch_size: PositiveInt = 100
    relay_claim_seconds: PositiveInt = 30
    relay_retry_seconds: NonNegativeInt = 5
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
