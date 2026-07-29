(function initAgentMeshWorld(global) {
  "use strict";

  const SCHEMA = "agentmesh.office-map.v1";
  const MAX_EXPANSIONS = 4096;

  const fallbackCampus = {
    schema: SCHEMA,
    name: "AgentMesh Campus",
    width: 52,
    height: 30,
    tilewidth: 64,
    tileheight: 64,
    layers: [
      {
        name: "navigation",
        type: "objectgroup",
        objects: [{ id: 1, name: "fallback-floor", type: "walkable", x: 0, y: 0, width: 3328, height: 1920 }]
      },
      { name: "collision", type: "objectgroup", objects: [] },
      {
        name: "zones",
        type: "objectgroup",
        objects: [{ id: 2, name: "campus", type: "zone", x: 0, y: 0, width: 3328, height: 1920 }]
      },
      { name: "stations", type: "objectgroup", objects: [] },
      { name: "portals", type: "objectgroup", objects: [] }
    ],
    properties: [{ name: "version", type: "string", value: "fallback" }]
  };

  function layer(map, name) {
    return map.layers.find((item) => item.name === name);
  }

  function property(object, name, fallback = null) {
    return object.properties?.find((item) => item.name === name)?.value ?? fallback;
  }

  function validateCampus(map) {
    const errors = [];
    if (!map || map.schema !== SCHEMA) errors.push(`schema must be ${SCHEMA}`);
    for (const key of ["width", "height", "tilewidth", "tileheight"]) {
      if (!Number.isInteger(map?.[key]) || map[key] <= 0) errors.push(`${key} must be a positive integer`);
    }
    for (const name of ["navigation", "collision", "zones", "stations", "portals"]) {
      const candidate = map?.layers?.find((item) => item.name === name);
      if (!candidate || candidate.type !== "objectgroup") errors.push(`missing object layer: ${name}`);
    }
    const knownIds = new Set();
    for (const candidate of map?.layers || []) {
      for (const object of candidate.objects || []) {
        if (!Number.isInteger(object.id) || knownIds.has(object.id)) errors.push(`invalid or duplicate object id: ${object.id}`);
        knownIds.add(object.id);
        if ([object.x, object.y, object.width, object.height].some((value) => value != null && (!Number.isFinite(value) || value < 0))) {
          errors.push(`invalid bounds for object: ${object.name || object.id}`);
        }
      }
    }
    if (errors.length) throw new Error(errors.join("; "));
    return map;
  }

  function compileCampus(map) {
    validateCampus(map);
    const columns = map.width;
    const rows = map.height;
    const walkable = new Uint8Array(columns * rows);
    const setRectangle = (object, value) => {
      const left = Math.max(0, Math.floor(object.x / map.tilewidth));
      const top = Math.max(0, Math.floor(object.y / map.tileheight));
      const right = Math.min(columns - 1, Math.ceil((object.x + object.width) / map.tilewidth) - 1);
      const bottom = Math.min(rows - 1, Math.ceil((object.y + object.height) / map.tileheight) - 1);
      for (let row = top; row <= bottom; row += 1) {
        for (let column = left; column <= right; column += 1) walkable[row * columns + column] = value;
      }
    };
    for (const object of layer(map, "navigation").objects) setRectangle(object, 1);
    for (const object of layer(map, "collision").objects) setRectangle(object, 0);
    const zones = layer(map, "zones").objects.map((object) => ({
      id: object.name,
      label: property(object, "label", object.name),
      floor: property(object, "floor", "hq"),
      x: object.x,
      y: object.y,
      width: object.width,
      height: object.height
    }));
    const stations = new Map(layer(map, "stations").objects.map((object) => [
      object.name,
      {
        x: object.x,
        y: object.y,
        department: property(object, "department", "operations")
      }
    ]));
    return {
      map,
      columns,
      rows,
      worldWidth: columns * map.tilewidth,
      worldHeight: rows * map.tileheight,
      walkable,
      zones,
      stations,
      portals: layer(map, "portals").objects
    };
  }

  function cellKey(column, row) {
    return `${column}:${row}`;
  }

  function worldToCell(compiled, point) {
    return {
      column: Math.max(0, Math.min(compiled.columns - 1, Math.floor(point.x / compiled.map.tilewidth))),
      row: Math.max(0, Math.min(compiled.rows - 1, Math.floor(point.y / compiled.map.tileheight)))
    };
  }

  function cellToWorld(compiled, cell) {
    return {
      x: (cell.column + 0.5) * compiled.map.tilewidth,
      y: (cell.row + 0.5) * compiled.map.tileheight
    };
  }

  function isWalkable(compiled, cell) {
    return cell.column >= 0 && cell.row >= 0
      && cell.column < compiled.columns && cell.row < compiled.rows
      && compiled.walkable[cell.row * compiled.columns + cell.column] === 1;
  }

  function nearestWalkable(compiled, origin) {
    if (isWalkable(compiled, origin)) return origin;
    const queue = [origin];
    const seen = new Set([cellKey(origin.column, origin.row)]);
    while (queue.length && seen.size <= MAX_EXPANSIONS) {
      const current = queue.shift();
      for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
        const next = { column: current.column + dx, row: current.row + dy };
        const key = cellKey(next.column, next.row);
        if (seen.has(key) || next.column < 0 || next.row < 0 || next.column >= compiled.columns || next.row >= compiled.rows) continue;
        if (isWalkable(compiled, next)) return next;
        seen.add(key);
        queue.push(next);
      }
    }
    return null;
  }

  function reconstruct(cameFrom, current) {
    const path = [current];
    let cursor = current;
    while (cameFrom.has(cellKey(cursor.column, cursor.row))) {
      cursor = cameFrom.get(cellKey(cursor.column, cursor.row));
      path.push(cursor);
    }
    return path.reverse();
  }

  function simplify(path) {
    if (path.length < 3) return path;
    const result = [path[0]];
    for (let index = 1; index < path.length - 1; index += 1) {
      const previous = path[index - 1];
      const current = path[index];
      const next = path[index + 1];
      const before = [current.column - previous.column, current.row - previous.row];
      const after = [next.column - current.column, next.row - current.row];
      if (before[0] !== after[0] || before[1] !== after[1]) result.push(current);
    }
    result.push(path[path.length - 1]);
    return result;
  }

  function findPath(compiled, startPoint, endPoint) {
    const start = nearestWalkable(compiled, worldToCell(compiled, startPoint));
    const goal = nearestWalkable(compiled, worldToCell(compiled, endPoint));
    if (!start || !goal) return [];
    const open = [start];
    const openKeys = new Set([cellKey(start.column, start.row)]);
    const cameFrom = new Map();
    const scores = new Map([[cellKey(start.column, start.row), 0]]);
    const estimate = (cell) => Math.abs(cell.column - goal.column) + Math.abs(cell.row - goal.row);
    let expansions = 0;
    while (open.length && expansions < MAX_EXPANSIONS) {
      open.sort((left, right) => (
        (scores.get(cellKey(left.column, left.row)) + estimate(left))
        - (scores.get(cellKey(right.column, right.row)) + estimate(right))
      ));
      const current = open.shift();
      openKeys.delete(cellKey(current.column, current.row));
      if (current.column === goal.column && current.row === goal.row) {
        return simplify(reconstruct(cameFrom, current)).map((cell) => cellToWorld(compiled, cell));
      }
      expansions += 1;
      for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
        const next = { column: current.column + dx, row: current.row + dy };
        if (!isWalkable(compiled, next)) continue;
        const nextKey = cellKey(next.column, next.row);
        const tentative = (scores.get(cellKey(current.column, current.row)) ?? Infinity) + 1;
        if (tentative >= (scores.get(nextKey) ?? Infinity)) continue;
        cameFrom.set(nextKey, current);
        scores.set(nextKey, tentative);
        if (!openKeys.has(nextKey)) {
          open.push(next);
          openKeys.add(nextKey);
        }
      }
    }
    return [];
  }

  function zoneForPoint(compiled, point) {
    return compiled.zones.find((zone) => (
      point.x >= zone.x && point.x <= zone.x + zone.width
      && point.y >= zone.y && point.y <= zone.y + zone.height
    )) || null;
  }

  global.AgentMeshWorld = Object.freeze({
    SCHEMA,
    fallbackCampus,
    validateCampus,
    compileCampus,
    worldToCell,
    cellToWorld,
    isWalkable,
    nearestWalkable,
    findPath,
    zoneForPoint
  });
}(globalThis));
