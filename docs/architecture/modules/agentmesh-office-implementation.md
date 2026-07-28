# AgentMesh Office spatial Console implementation

Status: Implemented

## Outcome

`/world` is a separate spatial operator surface linked from the main Console. It presents the
tenant as a pixel-art technology company: Agent Definitions are employees, inferred role groups
are departments, active Runs are work assignments, dependency and Handoff edges are collaboration
routes, and durable Handoffs animate one employee walking to another.

The scene is not a second orchestrator. It never advances Tasks, invents Agent state, or persists
game-only progress. PostgreSQL-backed Agent, Task, Run, Subtask, and Handoff records remain
authoritative.

## Rendering boundary

- Phaser 3.90 owns the central Canvas scene, employee objects, input hit areas, route packets,
  runtime-generated four-direction sprite sheet, environment effects, and Handoff walking
  sequences.
- A clipped DOM map layer owns the high-resolution pixel background and is transformed from the
  same Phaser camera state. This keeps the raster crisp even on software-rendered test hosts while
  preserving one coordinate system for employees, routes, and the map.
- HTML and CSS own navigation, the Task mission board, connection settings, employee inspector,
  language controls, and accessible textual status.
- The Phaser MIT distribution is pinned and self-hosted under Console assets, so the page remains
  offline-capable and compatible with the existing self-only Content Security Policy.
- The original office background is a project-owned raster asset. No assets from an existing game
  are used.
- `world-campus.json` is a Tiled-compatible, versioned semantic map. Its navigation, collision,
  zone, station, and portal object layers are validated in CI.
- `world-tiles.svg`, `world-employee.png`, and `world-assets.json` record the project-owned visual
  sources and provenance. The employee PNG is reproducible with
  `scripts/generate_world_employee.py`.

## State projection

1. The page reads Feature Gates and tenant-scoped Task aggregates through the existing Control API.
2. With Agent Registry enabled, published definitions and versions provide employee metadata.
3. Runtime Agent IDs observed in Task Runs are represented even when registry management is
   disabled.
4. Employee state is derived from Task and Run status: idle, working, waiting, blocked, or recently
   complete.
5. Subtask dependencies and persisted Handoffs produce visible routes.
6. Realtime domain notifications trigger an authoritative reload; polling remains the fallback.
7. Selecting an employee exposes its real version, lifecycle, capabilities, Tool allowlist, and
   current Run without copying configuration into browser-owned state.
8. A persisted Handoff is the only event that starts employee travel. Bounded A* finds a route
   through the checked-in walkability grid; changing the selected Task cancels active travel.

## Exploration controls

- The office is a bounded 3328 x 1920 multi-screen world rather than a fixed dashboard backdrop.
- Operators can pan with WASD or arrow keys, drag the map, zoom with the mouse wheel or HUD
  controls, return to the campus center, and focus the selected employee.
- A clickable minimap shows the current camera viewport and supports direct navigation.
- Camera position and zoom are reflected in accessible HTML so the map remains inspectable and
  testable without treating visual state as authoritative domain state.
- Department views load on demand from the zone selector and preserve the selected employee.
- The employee selector provides a non-spatial roster fallback. More than 50 employees are
  represented with per-department overflow clusters to keep Canvas work bounded.
- Reduced-motion mode teleports Handoffs while retaining their evidence card. Ambient sound is a
  quiet Web Audio layer that starts only after explicit operator opt-in.

## Optional asset candidates

`scripts/office_asset_candidate.py` runs optional visual generators outside the application path
with a timeout, bounded retries, exponential backoff, and a provenance manifest. It writes only a
candidate and never replaces a checked-in asset. Local startup and CI never call an image service.

## Deliberate exclusions

- no experience points, levels, fictional skills, morale, currency, or automatic growth;
- no browser-owned Task progression;
- no free-form employee control, employee-to-employee collision simulation, or browser-authored
  employee movement;
- no third-party CDN, external telemetry, or sound autoplay;
- no attempt to reproduce the art, maps, characters, or UI of another game.
