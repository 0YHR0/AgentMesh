from __future__ import annotations

from typing import Protocol

from agentmesh.domain.office import (
    InvalidOfficeSpace,
    OfficeCellOccupied,
    OfficePlacement,
    OfficeSpace,
)


class OfficeLayoutStore(Protocol):
    def list(self, tenant_id: str) -> tuple[OfficePlacement, ...]: ...

    def get_at_cell(
        self, tenant_id: str, grid_x: int, grid_z: int
    ) -> OfficePlacement | None: ...

    def put(self, placement: OfficePlacement) -> None: ...

    def list_spaces(self, tenant_id: str) -> tuple[OfficeSpace, ...]: ...

    def put_space(self, space: OfficeSpace) -> None: ...

    def delete_spaces(self, tenant_id: str) -> int: ...


class OfficeLayoutService:
    def __init__(self, *, store: OfficeLayoutStore, tenant_id: str) -> None:
        self._store = store
        self._tenant_id = tenant_id

    def list_placements(self) -> tuple[OfficePlacement, ...]:
        return self._store.list(self._tenant_id)

    def list_spaces(self) -> tuple[OfficeSpace, ...]:
        return self._store.list_spaces(self._tenant_id)

    def create_space(
        self,
        *,
        key: str,
        name: str,
        style: str,
        color: str,
    ) -> OfficeSpace:
        spaces = self._store.list_spaces(self._tenant_id)
        if len(spaces) >= 8:
            raise InvalidOfficeSpace("the Office supports up to eight custom spaces")
        space = OfficeSpace.create(
            tenant_id=self._tenant_id,
            key=key,
            name=name,
            style=style,
            color=color,
            position=len(spaces),
        )
        self._store.put_space(space)
        return space

    def reset_spaces(self) -> int:
        return self._store.delete_spaces(self._tenant_id)

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
