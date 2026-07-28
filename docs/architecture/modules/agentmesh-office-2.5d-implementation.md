# AgentMesh Office 2.5D implementation

Status: Experimental, feature-gated

Feature Gate: `office_3d`

## Outcome

`/world-3d` is an original bright 2.5D strategy-style view of the same AgentMesh company projected
by `/world`. Babylon.js renders an orthographic campus, department buildings, Agent employees,
lighting, camera movement, and durable Handoff travel. Accessible DOM panels render missions,
status labels, the roster, and real Agent configuration.

The renderer is read-only. It does not own a second state machine and cannot mutate Task, Run,
Subtask, Handoff, Approval, Tool, or Agent records.

## Runtime and dependency boundary

- Babylon.js 9.5.0 UMD is pinned under Console assets for the repository's zero-build runtime.
- The upstream Apache-2.0 license and notice are checked in beside the distribution.
- Production startup makes no Babylon CDN request.
- WebGL 2 is preferred and WebGL 1 remains available through Babylon's engine selection.
- `/world` remains the default Canvas-compatible renderer.

## Clarity and camera

- The campus uses scene geometry rather than a scaled full-map raster.
- An orthographic camera keeps the strategy-map composition stable while zooming.
- the Engine adapts to device pixel ratio up to a bounded 1.75 scale;
- Agent names and runtime status remain screen-space DOM labels;
- camera drag, WASD/arrows, wheel/buttons, home, Agent focus, and department minimap controls share
  one Babylon camera target.

## Performance behavior

The renderer projects at most 50 Agent meshes. In `auto` quality it samples sustained frame rate;
below 28 FPS it switches to `eco`, increases hardware scaling, and stops non-essential environment
rotation. Operators may toggle the same mode explicitly. This changes only rendering quality.

## Enable

The experiment is intentionally excluded from `minimal`, `standard`, and `full` profiles:

```bash
AGENTMESH_FEATURE_GATES=office_3d=true
```

When other overrides are needed, include them in the same comma-separated value.
