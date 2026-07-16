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
    database_url: str = (
        "postgresql+psycopg://agentmesh:agentmesh@127.0.0.1:5432/agentmesh"
    )
    checkpoint_database_url: str = (
        "postgresql://agentmesh:agentmesh@127.0.0.1:5432/agentmesh"
    )
    agent_id: str = "demo-agent"
    langfuse_enabled: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
