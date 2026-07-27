from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4


@dataclass(frozen=True)
class ReplayBookmark:
    id: UUID
    tenant_id: str
    task_id: UUID
    event_id: str
    label: str
    created_by: str
    created_at: datetime

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        task_id: UUID,
        event_id: str,
        label: str,
        created_by: str,
    ) -> ReplayBookmark:
        normalized_event_id = event_id.strip()
        normalized_label = label.strip()
        if not normalized_event_id or len(normalized_event_id) > 255:
            raise ValueError("event_id must contain between 1 and 255 characters")
        if not normalized_label or len(normalized_label) > 120:
            raise ValueError("label must contain between 1 and 120 characters")
        return cls(
            id=uuid4(),
            tenant_id=tenant_id,
            task_id=task_id,
            event_id=normalized_event_id,
            label=normalized_label,
            created_by=created_by,
            created_at=datetime.now(timezone.utc),
        )
