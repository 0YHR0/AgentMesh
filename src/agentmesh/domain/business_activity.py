"""Explicit, bounded employee activity notices supplied by trusted extensions.

Only the summary is intended for external collaboration channels; never pass raw
prompts, memory, model reasoning, credentials, or provider responses here.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ACTIVITY_SCHEMA = "agentmesh.company.object.activity"


class BusinessActivityNotice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    agent_id: str = Field(min_length=1, max_length=128)
    role_label: str = Field(min_length=1, max_length=80)
    phase: Literal["STARTED", "RESULT", "FAILED"]
    summary: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("agent_id", "role_label", "summary")
    @classmethod
    def normalized_text(cls, value: str | None) -> str | None:
        if value is not None and (value != value.strip() or any(ord(c) < 32 for c in value)):
            raise ValueError("Activity text must be normalized and contain no control characters")
        return value
