from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


class InvalidOfficePlacement(ValueError):
    pass


class OfficeCellOccupied(ValueError):
    pass


@dataclass(frozen=True)
class OfficeGrid:
    cell_size: int = 2
    origin_x: int = -35
    origin_z: int = -12
    columns: int = 35
    rows: int = 12


@dataclass(frozen=True)
class OfficeRoom:
    key: str
    grid_x: int
    grid_z: int
    width: int = 8
    depth: int = 5

    def contains(self, grid_x: int, grid_z: int) -> bool:
        return (
            self.grid_x <= grid_x < self.grid_x + self.width
            and self.grid_z <= grid_z < self.grid_z + self.depth
        )


DEFAULT_OFFICE_GRID = OfficeGrid()
DEFAULT_OFFICE_ROOMS = (
    OfficeRoom("product", 0, 0),
    OfficeRoom("research", 9, 0),
    OfficeRoom("analysis", 18, 0),
    OfficeRoom("security", 27, 0),
    OfficeRoom("design", 0, 7),
    OfficeRoom("engineering", 9, 7),
    OfficeRoom("operations", 18, 7),
    OfficeRoom("commons", 27, 7),
)


def department_for_cell(grid_x: int, grid_z: int) -> str:
    for room in DEFAULT_OFFICE_ROOMS:
        if room.contains(grid_x, grid_z):
            return room.key
    raise InvalidOfficePlacement(
        f"Office cell ({grid_x}, {grid_z}) is not inside a department"
    )


@dataclass(frozen=True)
class OfficePlacement:
    tenant_id: str
    agent_id: str
    grid_x: int
    grid_z: int
    department: str
    updated_at: datetime

    @classmethod
    def place(
        cls,
        *,
        tenant_id: str,
        agent_id: str,
        grid_x: int,
        grid_z: int,
    ) -> OfficePlacement:
        if not tenant_id.strip():
            raise InvalidOfficePlacement("tenant_id is required")
        normalized_agent_id = agent_id.strip()
        if not normalized_agent_id or len(normalized_agent_id) > 255:
            raise InvalidOfficePlacement("agent_id must contain 1 to 255 characters")
        grid = DEFAULT_OFFICE_GRID
        if not 0 <= grid_x < grid.columns or not 0 <= grid_z < grid.rows:
            raise InvalidOfficePlacement(
                f"Office cell ({grid_x}, {grid_z}) is outside the campus grid"
            )
        return cls(
            tenant_id=tenant_id,
            agent_id=normalized_agent_id,
            grid_x=grid_x,
            grid_z=grid_z,
            department=department_for_cell(grid_x, grid_z),
            updated_at=datetime.now(timezone.utc),
        )
