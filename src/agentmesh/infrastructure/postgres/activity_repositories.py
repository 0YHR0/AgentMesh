from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from agentmesh.domain.activity import ReplayBookmark
from agentmesh.infrastructure.postgres.models import ReplayBookmarkRecord


class SqlAlchemyReplayBookmarkRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, bookmark: ReplayBookmark) -> None:
        statement = (
            insert(ReplayBookmarkRecord)
            .values(
                id=bookmark.id,
                tenant_id=bookmark.tenant_id,
                task_id=bookmark.task_id,
                event_id=bookmark.event_id,
                label=bookmark.label,
                created_by=bookmark.created_by,
                created_at=bookmark.created_at,
            )
            .on_conflict_do_nothing(
                constraint="uq_replay_bookmark_task_event",
            )
        )
        self._session.execute(statement)

    def get(self, bookmark_id: UUID) -> ReplayBookmark | None:
        record = self._session.get(ReplayBookmarkRecord, bookmark_id)
        return self._to_domain(record) if record is not None else None

    def find_for_event(
        self, *, tenant_id: str, task_id: UUID, event_id: str
    ) -> ReplayBookmark | None:
        statement = select(ReplayBookmarkRecord).where(
            ReplayBookmarkRecord.tenant_id == tenant_id,
            ReplayBookmarkRecord.task_id == task_id,
            ReplayBookmarkRecord.event_id == event_id,
        )
        record = self._session.scalar(statement)
        return self._to_domain(record) if record is not None else None

    def list_for_task(self, *, tenant_id: str, task_id: UUID) -> list[ReplayBookmark]:
        statement = (
            select(ReplayBookmarkRecord)
            .where(
                ReplayBookmarkRecord.tenant_id == tenant_id,
                ReplayBookmarkRecord.task_id == task_id,
            )
            .order_by(ReplayBookmarkRecord.created_at.desc(), ReplayBookmarkRecord.id.desc())
        )
        return [self._to_domain(record) for record in self._session.scalars(statement)]

    def delete(self, bookmark_id: UUID) -> None:
        self._session.execute(
            delete(ReplayBookmarkRecord).where(ReplayBookmarkRecord.id == bookmark_id)
        )

    @staticmethod
    def _to_domain(record: ReplayBookmarkRecord) -> ReplayBookmark:
        return ReplayBookmark(
            id=record.id,
            tenant_id=record.tenant_id,
            task_id=record.task_id,
            event_id=record.event_id,
            label=record.label,
            created_by=record.created_by,
            created_at=record.created_at,
        )
