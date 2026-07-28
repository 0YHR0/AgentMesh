# AgentMesh Office 2.5D renderer

Status: Implemented experimental renderer

Feature Gate: `office_3d` (explicit opt-in, including with the `full` profile)

## Problem

The stable Phaser Office is intentionally compatible with Canvas-only and constrained devices,
but a raster campus cannot remain equally sharp at arbitrary camera scales. Mixing screen-space
operator UI with a scaled world image also limits depth, lighting, and spatial legibility.

## Decision

Add `/world-3d` as a separate, feature-gated Babylon.js renderer. It reuses the existing Control
API and authoritative Task, Run, Subtask, Handoff, Agent Definition, and Agent Version data.
`/world` remains the default lightweight renderer and fallback.

The visual direction is an original bright 2.5D mobile-strategy office. It may use readable
silhouettes, orthographic composition, saturated department colors, and restrained animation, but
must not copy another game's characters, arenas, buildings, icons, names, sounds, or textures.

Each department must be identifiable by spatial function rather than color alone:

- Research uses an observatory, scanner, telescope, and sample pods.
- Analysis uses a data tower, animated metric bars, and a holographic roundtable.
- Engineering uses a workshop, sawtooth roof, conveyor, and bounded robot-arm motion.
- Operations uses a tiered review court, decision dais, command board, and verdict beacon.

Shared paths, route lights, trees, lamps, and the central Handoff Nexus make the four areas one
company campus. Crisp DOM department plaques remain readable independently of world zoom.

## Rendering boundary

- Babylon.js 9.5.0 is self-hosted with its Apache-2.0 license and no production CDN dependency.
- An orthographic camera, geometry, lighting, and antialiasing render the world.
- Agent names, runtime status, mission controls, and inspector content remain DOM overlays so they
  stay sharp and accessible at every supported world zoom.
- Hardware scaling is bounded by device pixel ratio and may degrade automatically when sustained
  frame rate is low.
- WebGL is the compatibility baseline. WebGPU may be evaluated later without changing domain data.

## Runtime truth

- Agent meshes project real Agent Definitions or runtime Agent IDs.
- Status derives only from persisted Task and Run state.
- Routes and movement begin only after a durable Handoff is visible through the existing API.
- The renderer cannot create, advance, approve, cancel, or mutate work.

## Interaction

- pointer drag and WASD/arrow keys pan the orthographic camera;
- wheel and HUD buttons zoom;
- clicking a mesh or roster item selects and focuses an Agent;
- department buttons focus bounded office zones;
- a minimap and the stable `/world` link provide recovery paths;
- English and Simplified Chinese use the same saved language preference.

## Acceptance criteria

- the route returns a feature-disabled response unless `office_3d=true`;
- the scene loads without a third-party request;
- map and Agents share one Babylon scene and camera;
- DOM status labels remain legible during camera zoom;
- selection and Task state survive camera movement;
- a low-quality mode reduces pixel ratio and non-essential animation;
- every department has a unique silhouette, facility set, plaque, and restrained signature motion;
- the existing Phaser Office remains unchanged and available.

## Enable

```bash
AGENTMESH_FEATURE_GATES=office_3d=true
```

With the `full` profile, append the same explicit override because experimental GPU rendering is
not part of any built-in profile.
