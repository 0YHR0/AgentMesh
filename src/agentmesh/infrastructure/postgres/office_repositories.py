from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.domain.office import (
    InvalidOfficeSpace,
    OfficeCellOccupied,
    OfficePlacement,
    OfficeSpace,
)
from agentmesh.infrastructure.postgres.models import (
    OfficePlacementRecord,
    OfficeSpaceRecord,
)


class SqlAlchemyOfficePlacementStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list(self, tenant_id: str) -> tuple[OfficePlacement, ...]:
        with self._session_factory() as session:
            records = session.scalars(
                select(OfficePlacementRecord)
                .where(OfficePlacementRecord.tenant_id == tenant_id)
                .order_by(OfficePlacementRecord.agent_id)
            )
            return tuple(self._placement(record) for record in records)

    def get_at_cell(
        self, tenant_id: str, grid_x: int, grid_z: int
    ) -> OfficePlacement | None:
        with self._session_factory() as session:
            record = session.scalar(
                select(OfficePlacementRecord).where(
                    OfficePlacementRecord.tenant_id == tenant_id,
                    OfficePlacementRecord.grid_x == grid_x,
                    OfficePlacementRecord.grid_z == grid_z,
                )
            )
            return self._placement(record) if record is not None else None

    def put(self, placement: OfficePlacement) -> None:
        statement = insert(OfficePlacementRecord).values(
            tenant_id=placement.tenant_id,
            agent_id=placement.agent_id,
            grid_x=placement.grid_x,
            grid_z=placement.grid_z,
            department=placement.department,
            updated_at=placement.updated_at,
        )
        statement = statement.on_conflict_do_update(
            index_elements=["tenant_id", "agent_id"],
            set_={
                "grid_x": placement.grid_x,
                "grid_z": placement.grid_z,
                "department": placement.department,
                "updated_at": placement.updated_at,
            },
        )
        try:
            with self._session_factory.begin() as session:
                session.execute(statement)
        except IntegrityError as exc:
            raise OfficeCellOccupied(
                f"Office cell ({placement.grid_x}, {placement.grid_z}) was occupied concurrently"
            ) from exc

    def list_spaces(self, tenant_id: str) -> tuple[OfficeSpace, ...]:
        with self._session_factory() as session:
            records = session.scalars(
                select(OfficeSpaceRecord)
                .where(OfficeSpaceRecord.tenant_id == tenant_id)
                .order_by(OfficeSpaceRecord.position)
            )
            return tuple(self._space(record) for record in records)

    def put_space(self, space: OfficeSpace) -> None:
        try:
            with self._session_factory.begin() as session:
                session.add(
                    OfficeSpaceRecord(
                        tenant_id=space.tenant_id,
                        key=space.key,
                        name=space.name,
                        style=space.style,
                        color=space.color,
                        position=space.position,
                        created_at=space.created_at,
                        updated_at=space.updated_at,
                    )
                )
        except IntegrityError as exc:
            raise InvalidOfficeSpace(
                "the shared Office layout changed concurrently; reload and retry"
            ) from exc

    def delete_spaces(self, tenant_id: str) -> int:
        with self._session_factory.begin() as session:
            result = session.execute(
                delete(OfficeSpaceRecord).where(
                    OfficeSpaceRecord.tenant_id == tenant_id
                )
            )
            return result.rowcount or 0

    @staticmethod
    def _placement(record: OfficePlacementRecord) -> OfficePlacement:
        return OfficePlacement(
            tenant_id=record.tenant_id,
            agent_id=record.agent_id,
            grid_x=record.grid_x,
            grid_z=record.grid_z,
            department=record.department,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _space(record: OfficeSpaceRecord) -> OfficeSpace:
        return OfficeSpace(
            tenant_id=record.tenant_id,
            key=record.key,
            name=record.name,
            style=record.style,
            color=record.color,
            position=record.position,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
