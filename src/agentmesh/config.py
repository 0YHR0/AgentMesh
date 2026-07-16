from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTMESH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
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
    worker_block_ms: int = 1_000
    worker_pending_idle_ms: int = 60_000
    run_lease_seconds: int = 300
    relay_batch_size: int = 100
    relay_claim_seconds: int = 30
    relay_retry_seconds: int = 5
    langfuse_enabled: bool = False
    feature_profile: str = "minimal"
    feature_gates: str = ""
    artifact_owner_id: str = "local-user"
    artifact_max_inline_bytes: int = 65_536
    mcp_workspace_root: str = "."
    mcp_workspace_timeout_seconds: int = 30
    mcp_workspace_max_bytes: int = 65_536
    mcp_max_result_bytes: int = 262_144


@lru_cache
def get_settings() -> Settings:
    return Settings()
