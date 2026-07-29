from __future__ import annotations

from typing import Protocol

from agentmesh.domain.office import OfficeCellOccupied, OfficePlacement


class OfficePlacementStore(Protocol):
    def list(self, tenant_id: str) -> tuple[OfficePlacement, ...]: ...

    def get_at_cell(
        self, tenant_id: str, grid_x: int, grid_z: int
    ) -> OfficePlacement | None: ...

    def put(self, placement: OfficePlacement) -> None: ...


class OfficeLayoutService:
    def __init__(self, *, store: OfficePlacementStore, tenant_id: str) -> None:
        self._store = store
        self._tenant_id = tenant_id

    def list_placements(self) -> tuple[OfficePlacement, ...]:
        return self._store.list(self._tenant_id)

    def place_employee(
        self, *, agent_id: str, grid_x: int, grid_z: int
    ) -> OfficePlacement:
        placement = OfficePlacement.place(
            tenant_id=self._tenant_id,
            agent_id=agent_id,
            grid_x=grid_x,
            grid_z=grid_z,
        )
        occupant = self._store.get_at_cell(self._tenant_id, grid_x, grid_z)
        if occupant is not None and occupant.agent_id != placement.agent_id:
            raise OfficeCellOccupied(
                f"Office cell ({grid_x}, {grid_z}) is occupied by '{occupant.agent_id}'"
            )
        self._store.put(placement)
        return placement
