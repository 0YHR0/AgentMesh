from functools import lru_cache
from uuid import UUID

from pydantic import (
    AliasChoices,
    Field,
    NonNegativeInt,
    PositiveInt,
    field_validator,
    model_validator,
)
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
    reviewer_agent_id: str = "demo-reviewer"
    review_max_revisions: int = Field(default=3, ge=0, le=10)
    supervisor_agent_id: str = "demo-supervisor"
    coordinated_max_concurrency: int = Field(default=4, ge=1, le=10)
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
    identity_principals_json: str = "[]"
    identity_oidc_issuer: str | None = None
    identity_oidc_audience: str | None = None
    identity_oidc_jwks_cache_seconds: PositiveInt = 300
    policy_rules_json: str = ""
    policy_action_ttl_seconds: PositiveInt = 3_600
    artifact_owner_id: str = "local-user"
    artifact_max_inline_bytes: PositiveInt = 65_536
    mcp_workspace_root: str = "."
    mcp_workspace_timeout_seconds: PositiveInt = 30
    mcp_workspace_max_bytes: PositiveInt = 65_536
    mcp_max_result_bytes: PositiveInt = 262_144
    mcp_http_timeout_seconds: int = Field(default=30, ge=1, le=300)
    a2a_timeout_seconds: PositiveInt = 30
    a2a_max_request_bytes: PositiveInt = 65_536
    a2a_max_response_bytes: PositiveInt = 262_144
    a2a_max_inline_result_bytes: PositiveInt = 65_536
    credential_workload_principal_id: UUID | None = None
    credential_lease_ttl_seconds: int = Field(default=60, ge=1, le=300)

    @field_validator("credential_workload_principal_id", mode="before")
    @classmethod
    def empty_optional_uuid_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def validate_messaging_retention_horizons(self) -> Self:
        if self.agent_id.strip().lower() == self.reviewer_agent_id.strip().lower():
            raise ValueError("agent_id and reviewer_agent_id must be distinct")
        agent_names = {
            self.agent_id.strip().lower(),
            self.reviewer_agent_id.strip().lower(),
            self.supervisor_agent_id.strip().lower(),
        }
        if len(agent_names) != 3:
            raise ValueError(
                "agent_id, reviewer_agent_id, and supervisor_agent_id must be distinct"
            )
        stream_names = {
            self.execution_stream,
            self.domain_event_stream,
            self.dead_letter_stream,
        }
        if len(stream_names) != 3:
            raise ValueError(
                "execution_stream, domain_event_stream, and dead_letter_stream must be distinct"
            )
        if self.outbox_retention_seconds < self.redis_stream_retention_seconds:
            raise ValueError(
                "outbox_retention_seconds must be greater than or equal to "
                "redis_stream_retention_seconds"
            )
        if self.inbox_retention_seconds < self.outbox_retention_seconds:
            raise ValueError(
                "inbox_retention_seconds must be greater than or equal to outbox_retention_seconds"
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
