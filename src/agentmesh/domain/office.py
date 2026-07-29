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


@dataclass(frozen=True)
class OfficeObstacle:
    grid_x: int
    grid_z: int
    kind: str = "furniture"


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
DEFAULT_OFFICE_OBSTACLES = (
    OfficeObstacle(1, 1, "furniture"),
    OfficeObstacle(5, 1, "equipment"),
    OfficeObstacle(10, 1, "observatory"),
    OfficeObstacle(11, 1, "observatory"),
    OfficeObstacle(15, 1, "sample-pod"),
    OfficeObstacle(15, 2, "sample-pod"),
    OfficeObstacle(15, 3, "sample-pod"),
    OfficeObstacle(19, 1, "data-display"),
    OfficeObstacle(24, 1, "data-tower"),
    OfficeObstacle(29, 1, "security-console"),
    OfficeObstacle(2, 9, "design-table"),
    OfficeObstacle(10, 9, "workshop"),
    OfficeObstacle(14, 8, "conveyor"),
    OfficeObstacle(19, 9, "review-seating"),
    OfficeObstacle(23, 8, "decision-dais"),
    OfficeObstacle(29, 9, "commons-table"),
)


def department_for_cell(grid_x: int, grid_z: int) -> str:
    for room in DEFAULT_OFFICE_ROOMS:
        if room.contains(grid_x, grid_z):
            return room.key
    raise InvalidOfficePlacement(
        f"Office cell ({grid_x}, {grid_z}) is not inside a department"
    )


def obstacle_for_cell(grid_x: int, grid_z: int) -> OfficeObstacle | None:
    return next(
        (
            obstacle
            for obstacle in DEFAULT_OFFICE_OBSTACLES
            if obstacle.grid_x == grid_x and obstacle.grid_z == grid_z
        ),
        None,
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
        obstacle = obstacle_for_cell(grid_x, grid_z)
        if obstacle is not None:
            raise InvalidOfficePlacement(
                f"Office cell ({grid_x}, {grid_z}) is reserved for {obstacle.kind}"
            )
        return cls(
            tenant_id=tenant_id,
            agent_id=normalized_agent_id,
            grid_x=grid_x,
            grid_z=grid_z,
            department=department_for_cell(grid_x, grid_z),
            updated_at=datetime.now(timezone.utc),
        )
