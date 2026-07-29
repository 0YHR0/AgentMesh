from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.domain.office import OfficeCellOccupied, OfficePlacement
from agentmesh.infrastructure.postgres.models import OfficePlacementRecord


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
