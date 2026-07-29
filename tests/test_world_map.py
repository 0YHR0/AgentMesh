import json
from collections import deque
from pathlib import Path

ASSET_DIRECTORY = (
    Path(__file__).parents[1]
    / "src"
    / "agentmesh"
    / "api"
    / "console_assets"
)


def _cells_for_rectangle(
    item: dict[str, object],
    *,
    columns: int,
    rows: int,
    tile_width: int,
    tile_height: int,
) -> set[tuple[int, int]]:
    left = max(0, int(item["x"]) // tile_width)
    top = max(0, int(item["y"]) // tile_height)
    right = min(columns - 1, (int(item["x"]) + int(item["width"]) - 1) // tile_width)
    bottom = min(rows - 1, (int(item["y"]) + int(item["height"]) - 1) // tile_height)
    return {
        (column, row)
        for row in range(top, bottom + 1)
        for column in range(left, right + 1)
    }


def test_office_map_is_valid_and_all_departments_are_reachable() -> None:
    campus = json.loads((ASSET_DIRECTORY / "world-campus.json").read_text())
    assert campus["schema"] == "agentmesh.office-map.v1"
    assert campus["orientation"] == "orthogonal"
    assert campus["tilesets"] == [{"firstgid": 1, "source": "world-tiles.tsx"}]

    layers = {layer["name"]: layer for layer in campus["layers"]}
    assert set(layers) == {"navigation", "collision", "zones", "stations", "portals"}
    assert all(layer["type"] == "objectgroup" for layer in layers.values())

    object_ids = [
        item["id"]
        for layer in layers.values()
        for item in layer["objects"]
    ]
    assert len(object_ids) == len(set(object_ids))

    columns = campus["width"]
    rows = campus["height"]
    tile_width = campus["tilewidth"]
    tile_height = campus["tileheight"]
    walkable: set[tuple[int, int]] = set()
    for item in layers["navigation"]["objects"]:
        walkable |= _cells_for_rectangle(
            item,
            columns=columns,
            rows=rows,
            tile_width=tile_width,
            tile_height=tile_height,
        )
    for item in layers["collision"]["objects"]:
        walkable -= _cells_for_rectangle(
            item,
            columns=columns,
            rows=rows,
            tile_width=tile_width,
            tile_height=tile_height,
        )

    start = min(walkable)
    reached = {start}
    queue = deque([start])
    while queue:
        column, row = queue.popleft()
        for candidate in (
            (column + 1, row),
            (column - 1, row),
            (column, row + 1),
            (column, row - 1),
        ):
            if candidate in walkable and candidate not in reached:
                reached.add(candidate)
                queue.append(candidate)

    assert reached == walkable
    for station in layers["stations"]["objects"]:
        cell = (station["x"] // tile_width, station["y"] // tile_height)
        assert cell in walkable, station["name"]
    assert {zone["name"] for zone in layers["zones"]["objects"]} == {
        "research",
        "analysis",
        "engineering",
        "operations",
        "hub",
    }


def test_office_tileset_assets_are_checked_in() -> None:
    tileset = (ASSET_DIRECTORY / "world-tiles.tsx").read_text()
    atlas = (ASSET_DIRECTORY / "world-tiles.svg").read_text()
    assert 'tilecount="4"' in tileset
    assert 'shape-rendering="crispEdges"' in atlas
