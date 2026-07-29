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
- Camera distance is independent from orthographic zoom and is sized beyond the largest supported
  campus diagonal; explicit near/far planes prevent lower rooms intersecting the camera when an
  operator zooms out and pans vertically.
- Pan limits include a zoom-dependent margin, keeping a useful portion of the campus in view at
  every supported scale.
- the Engine adapts to device pixel ratio up to a bounded 1.75 scale;
- Agent names and runtime status remain screen-space DOM labels;
- camera drag, WASD/arrows, wheel/buttons, home, Agent focus, and department minimap controls share
  one Babylon camera target.

## Department composition

The campus is one scene with four bounded, function-specific districts:

| Department | Spatial identity | Non-essential motion |
|---|---|---|
| Research Lab | Observatory dome, telescope, scanner and sample pods | Scanner and telescope sweep |
| Analysis Studio | Data tower, metric bars and holographic roundtable | Bar pulse and projection rotation |
| Engineering Bay | Workshop, sawtooth roof, conveyor and robot arms | Bounded robot-arm sweep |
| Review Court | Tiered seating, decision dais, command board and verdict beacon | Verdict rotation and hover |

Department identity cannot depend on hue alone. Silhouette, equipment, floor plan, and a
screen-space bilingual plaque provide redundant cues. Shared route lighting and amenities preserve
the visual model of one company rather than four disconnected maps.

The visual palette uses low-saturation departmental accents over neutral architectural materials.
Every room has shared architectural detail—walls, framed glass, entries, floor seams and furnished
workstations—while its functional equipment supplies identity. Soft directional shadows and
restrained emissive values add depth without returning to color-only differentiation.

Agent employees use a bounded low-poly character grammar with a tapered torso, collar and lapels,
department badge, face, articulated limb silhouettes, shoes, work tablet, selection ring and soft
ground shadow. These additions remain one lightweight mesh hierarchy per employee and do not
change runtime state.

The default layout includes eight spaces: Product, Research, Analysis, Security, Design,
Engineering, Operations, and People Commons. Layout bounds, the road grid, camera limits, labels,
and the minimap derive from the space collection instead of fixed map dimensions.

Operators may add up to eight personal custom spaces. The baseline persists these presentation
preferences in browser local storage and deterministically places them in additional campus rows.
Custom names also participate in Agent role/tag keyword placement. This is deliberately not a
shared domain model; server-synchronized layouts require a later authorized preference contract.

## Primary workflow

`/world-3d` is the daily operator surface. Its Task dialog sends the same validated
`CreateTaskRequest` used by the Admin Console and may issue the existing idempotent start command.
`/` is explicitly the Admin Console for Agent lifecycle, Tool, Approval, Artifact, identity, and
advanced Task controls.

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
