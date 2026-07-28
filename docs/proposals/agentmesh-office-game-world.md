# AgentMesh Office game-world evolution

Status: Proposed

## Context

The implemented Office baseline is an operational projection with an explorable multi-screen map,
camera pan/zoom, keyboard navigation, a clickable minimap, employee inspection, and live
Task/Run/Subtask/Handoff state. It deliberately reuses one self-hosted environment image and
procedural Phaser employees so the zero-build Console stays small and dependable.

This proposal defines the next game-like layer without turning AgentMesh into an unrelated
simulation or blocking product development on generated artwork.

## Outcome

Operators should feel that they are managing a living AI company:

- departments occupy distinct connected spaces;
- employees walk through valid corridors when durable work changes ownership;
- the camera can follow an employee, jump to an event, or move freely;
- visual detail stays crisp at supported zoom levels;
- every work animation remains a projection of persisted runtime evidence.

The world must not introduce experience points, morale, currency, or arbitrary progression.
Agent capability continues to come from Agent Definitions, Versions, Tools, policy, and observed
execution evidence.

## Proposed slices

### Slice 1 — deterministic tile map

- replace the single background image with a checked-in tile atlas and JSON map;
- use a fixed logical tile size and nearest-neighbor rendering;
- define walkable cells, walls, doors, desks, interaction points, department zones, and portals;
- validate the map in CI for missing tiles, unreachable departments, and invalid spawn points;
- retain the current image layer as a compatibility fallback.

Recommended authoring path: Tiled-compatible JSON plus a project-owned atlas. Phaser remains the
runtime; no separate frontend service is required.

### Slice 2 — navigation and handoff movement

- derive employee routes with bounded A* pathfinding over the checked-in walkability grid;
- walk from the current station to a meeting point or target employee only after the corresponding
  Run/Handoff event is persisted;
- cancel or re-plan movement when authoritative Task state changes;
- expose a reduced-motion mode that teleports employees and preserves the event card;
- never infer hidden conversation or model reasoning from movement.

### Slice 3 — sprite and environment polish

- add small project-owned employee sprite sheets with four-direction idle/walk states;
- add restrained environment animation for displays, doors, elevators, and the handoff hub;
- add department-specific desks and Tool interaction props;
- support optional, muted ambient sound only after an explicit user opt-in;
- keep employee status readable without relying on color or animation.

### Slice 4 — scalable campuses

- load independent floors or office zones on demand;
- preserve camera and employee selection across zone transitions;
- cluster or virtualize employees when definitions exceed the useful on-screen density;
- provide a low-motion list/inspector fallback for constrained devices.

## Non-blocking asset pipeline

Image generation is an optional design input, not a build dependency.

- generation jobs run outside the application request path;
- each attempt has a bounded timeout and records its prompt and outcome;
- retries use exponential backoff with a maximum attempt count;
- a failed or stalled generation leaves the last checked-in asset active;
- generated candidates require visual review, RGB re-encoding, license/provenance recording, and
  browser validation before they replace project assets;
- CI and local startup never call an image-generation service.

This avoids pausing engineering work when an external image job stalls.

## Acceptance criteria

- camera drag, WASD/arrow movement, wheel/buttons, center, focus, and minimap navigation work in
  current Chromium, Firefox, and Safari;
- the map stays sharp at documented zoom steps and does not require WebGL;
- 50 visible employees sustain 30 FPS on an ordinary integrated-GPU laptop;
- keyboard controls and buttons have accessible names, and reduced-motion behavior is complete;
- no animation claims a Run, Handoff, approval, Tool call, or A2A exchange that is absent from the
  authorized AgentMesh projections.

## Deferred decisions

- final tile size and atlas dimensions;
- whether world authoring remains Tiled-only or gains an AgentMesh-specific editor;
- collision behavior when many employees share a corridor;
- multi-floor transition presentation;
- whether optional sound ships as generated effects or project-recorded assets.
