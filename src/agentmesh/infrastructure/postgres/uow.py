from __future__ import annotations

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from agentmesh.domain.errors import ConcurrentTaskUpdate
from agentmesh.infrastructure.postgres.repositories import (
    SqlAlchemyIdempotencyRepository,
    SqlAlchemyInboxRepository,
    SqlAlchemyOutboxRepository,
    SqlAlchemyTaskAttemptRepository,
    SqlAlchemyTaskRepository,
    SqlAlchemyTaskRunRepository,
)


class SqlAlchemyUnitOfWork:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def __enter__(self) -> SqlAlchemyUnitOfWork:
        self._session = self._session_factory()
        self.tasks = SqlAlchemyTaskRepository(self._session)
        self.runs = SqlAlchemyTaskRunRepository(self._session)
        self.attempts = SqlAlchemyTaskAttemptRepository(self._session)
        self.outbox = SqlAlchemyOutboxRepository(self._session)
        self.inbox = SqlAlchemyInboxRepository(self._session)
        self.idempotency = SqlAlchemyIdempotencyRepository(self._session)
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if exc_type is not None:
            self.rollback()
        self._session.close()

    def commit(self) -> None:
        try:
            self._session.commit()
        except StaleDataError as exc:
            self._session.rollback()
            raise ConcurrentTaskUpdate("Task was modified by another transaction") from exc
        except SQLAlchemyError:
            self._session.rollback()
            raise

    def rollback(self) -> None:
        self._session.rollback()


class SqlAlchemyUnitOfWorkFactory:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def __call__(self) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(self._session_factory)
