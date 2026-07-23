# Agent Mission Map

Status: Slices 1-3 implemented

## Outcome

The Console should make durable Agent collaboration understandable at a glance. The recommended
surface is a two-dimensional mission-control map with restrained game-like presentation, backed by
authoritative Task ledgers rather than simulated chat or decorative animation.

The map answers four questions:

1. Which Agent owns each work item?
2. What is running, blocked, awaiting approval, or complete?
3. What durable information moved between Agents or external systems?
4. Why did the plan, status, or route change?

## Visual grammar

| Runtime concept | Map representation |
|---|---|
| Task / Goal Contract | Mission headquarters and objective banner |
| Agent-bound Subtask | Station with Agent avatar, role, status ring, and progress |
| DAG dependency | Solid route between stations |
| structured Handoff | Dashed contract route |
| durable output / Artifact | Document or package token moving along a route |
| Run dispatch | Command pulse from headquarters to a station |
| MCP Tool call | Uplink from a station to a Tool tower |
| A2A delegation | Portal to a remote mesh zone |
| Policy approval | Gate that visibly blocks a route |
| Plan Patch | Old unopened routes fade; verified replacement routes materialize |
| failure / retry | Red interruption marker followed by a new Attempt pulse |

Status colors must remain accessible and always include text or shape: queued is neutral, running
is cyan, completed is green, waiting is amber, and failed is red. Animation communicates a new
event only; it must never imply progress the backend has not persisted.

## Layout

Use a native SVG 2D/2.5D map before considering WebGL or a 3D world. SVG preserves the current
zero-build Console, stays readable on ordinary laptops, supports keyboard navigation, and makes
edges, tokens, labels, zoom, and selection straightforward.

The screen has three coordinated regions:

- **Mission Map**: stations, routes, gates, Tool towers, and remote portals.
- **Inspector**: selected Agent/Subtask/Run details, sanitized inputs/outputs, trace and evidence
  links, and available operator actions.
- **Event Deck**: chronological durable interactions with live mode and replay controls.

Operators can switch between Map and Audit views. The audit timeline remains the unambiguous source
for exact times and identifiers; the map is a spatial projection of the same evidence.

## Interaction event contract

The existing Task activity projection is sufficient for a timeline but does not always identify a
source node, target node, transport, or payload kind. The map should consume a bounded authorized
interaction projection with this shape:

```json
{
  "id": "handoff:<uuid>:accepted",
  "occurred_at": "2026-07-22T08:00:00Z",
  "kind": "HANDOFF_ACCEPTED",
  "source": {"type": "SUBTASK", "id": "..."},
  "target": {"type": "SUBTASK", "id": "..."},
  "transport": "HANDOFF",
  "payload_kind": "CONTEXT_CONTRACT",
  "status": "ACCEPTED",
  "trace_id": "...",
  "summary": {"handoff_id": "..."}
}
```

The server derives this from Task, Run, Attempt, dependency, Handoff, Artifact, Tool, Plan Patch,
approval, and A2A ledgers. Raw chain-of-thought, credentials, Tool arguments/results, and sensitive
Artifact content are never map events. SSE remains an invalidation signal; the authorized API is
the data plane and enables replay after reconnect.

## Delivery slices

### Slice 1 — truthful live map (implemented)

- native SVG DAG layout for the selected Task;
- Agent stations and status rings;
- dependency routes and animated Run/output pulses;
- click-to-inspect details;
- availability through the existing coordinated-execution profile and reduced-motion support.

### Slice 2 — governed interactions (implemented)

- Handoff routes, MCP towers, A2A portals, approval gates, and Plan Patch transitions are projected
  into the Mission Map interaction dock;
- `GET /api/v1/tasks/{task_id}/interactions` returns a bounded, tenant-scoped, permission-gated
  source/target contract derived from durable ledgers;
- Tool arguments/results, remote endpoints, approval arguments, Plan contents, and Handoff content
  are deliberately excluded from the projection;
- session-preserved filters narrow the map and Event Deck by transport, Agent, event kind, status,
  or trace ID.

### Slice 3 — replay and showcase (implemented)

- a stable `(occurred_at, event_id)` event order drives the time scrubber, pause/live controls,
  step controls, deterministic Run/Subtask state projection, and browser-local bookmarks;
- the packaged `examples/research-brief` fixture demonstrates retry, approval, Handoff, MCP Tool
  use, A2A remote state, and safe replanning without paid APIs or network traffic;
- exportable `agentmesh.mission-replay.v1` JSON contains Task metadata, redacted events,
  interaction projections, and bookmark event IDs for demos and incident review.

### Slice 4 — large-graph navigation (implemented baseline)

- bounded zoom controls, fit-to-viewport, one-to-one reset, and selected-node focus operate on the
  native SVG without changing authoritative execution data;
- pointer dragging pans the scrollable world, while Ctrl+wheel provides an optional precision
  zoom path and ordinary scrolling remains available;
- a clickable overview minimap projects headquarters, Agent stations, governed external nodes,
  and the current viewport for wide or deep DAGs;
- layouts beyond roughly one hundred visible nodes may still require semantic clustering or
  virtualization; navigation alone does not claim to solve graph-density limits.

## Guardrails

- Do not portray model hidden reasoning or invent Agent-to-Agent chat that did not occur.
- Do not let animation race ahead of persisted status.
- Do not require 3D hardware, audio, or continuous motion.
- Keep full keyboard navigation, high-contrast shapes, and `prefers-reduced-motion` behavior.
- Preserve the existing list/detail view as a low-motion operational fallback.
