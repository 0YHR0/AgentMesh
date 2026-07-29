from __future__ import annotations

import pytest

from agentmesh.application.office_services import OfficeLayoutService
from agentmesh.domain.office import (
    InvalidOfficePlacement,
    InvalidOfficeSpace,
    OfficeCellOccupied,
)
from tests.fakes import InMemoryOfficePlacementStore


def service() -> OfficeLayoutService:
    return OfficeLayoutService(
        store=InMemoryOfficePlacementStore(),
        tenant_id="test-tenant",
    )


def test_placement_derives_department_from_room_cell() -> None:
    layout = service()

    placement = layout.place_employee(agent_id="researcher", grid_x=9, grid_z=0)

    assert placement.department == "research"
    assert layout.list_placements() == (placement,)


def test_placement_rejects_corridor_cells() -> None:
    layout = service()

    with pytest.raises(InvalidOfficePlacement, match="not inside a department"):
        layout.place_employee(agent_id="researcher", grid_x=8, grid_z=0)


def test_placement_rejects_cells_reserved_for_furniture() -> None:
    layout = service()

    with pytest.raises(InvalidOfficePlacement, match="reserved for observatory"):
        layout.place_employee(agent_id="researcher", grid_x=10, grid_z=1)


def test_placement_prevents_two_agents_occupying_same_cell() -> None:
    layout = service()
    layout.place_employee(agent_id="researcher", grid_x=9, grid_z=0)

    with pytest.raises(OfficeCellOccupied, match="occupied by 'researcher'"):
        layout.place_employee(agent_id="analyst", grid_x=9, grid_z=0)


def test_employee_can_move_between_departments() -> None:
    layout = service()
    layout.place_employee(agent_id="researcher", grid_x=9, grid_z=0)

    moved = layout.place_employee(agent_id="researcher", grid_x=18, grid_z=0)

    assert moved.department == "analysis"
    assert layout.list_placements() == (moved,)


def test_custom_spaces_are_shared_in_deterministic_order_and_resettable() -> None:
    layout = service()

    first = layout.create_space(
        key="space-12345678",
        name="Customer Success",
        style="commons",
        color="#4FB8FF",
    )
    second = layout.create_space(
        key="space-87654321",
        name="Incident Room",
        style="security",
        color="#AA4455",
    )

    assert first.position == 0
    assert first.color == "#4fb8ff"
    assert second.position == 1
    assert layout.list_spaces() == (first, second)
    assert layout.reset_spaces() == 2
    assert layout.list_spaces() == ()


def test_custom_space_contract_rejects_unsupported_styles() -> None:
    layout = service()

    with pytest.raises(InvalidOfficeSpace, match="unsupported"):
        layout.create_space(
            key="space-12345678",
            name="Unknown",
            style="casino",
            color="#4fb8ff",
        )
