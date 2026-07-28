# AgentMesh Office spatial Console implementation

Status: Implemented MVP

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
  tweens, and Handoff walking sequences.
- A clipped DOM map layer owns the high-resolution pixel background and is transformed from the
  same Phaser camera state. This keeps the raster crisp even on software-rendered test hosts while
  preserving one coordinate system for employees, routes, and the map.
- HTML and CSS own navigation, the Task mission board, connection settings, employee inspector,
  language controls, and accessible textual status.
- The Phaser MIT distribution is pinned and self-hosted under Console assets, so the page remains
  offline-capable and compatible with the existing self-only Content Security Policy.
- The original office background is a project-owned raster asset. No assets from an existing game
  are used.

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

## Exploration controls

- The office is a bounded 3344 x 1882 multi-screen world rather than a fixed dashboard backdrop.
- Operators can pan with WASD or arrow keys, drag the map, zoom with the mouse wheel or HUD
  controls, return to the campus center, and focus the selected employee.
- A clickable minimap shows the current camera viewport and supports direct navigation.
- Camera position and zoom are reflected in accessible HTML so the map remains inspectable and
  testable without treating visual state as authoritative domain state.

## Deliberate exclusions

- no experience points, levels, fictional skills, morale, currency, or automatic growth;
- no browser-owned Task progression;
- no tile collision physics, free-form employee control, or browser-authored employee movement;
- no generated sound, third-party CDN, or external telemetry;
- no attempt to reproduce the art, maps, characters, or UI of another game.

Future versions may add a real tilemap, bounded pathfinding, department layout editing, and replay
controls while preserving the same projection-only boundary.
