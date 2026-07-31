# Simple core and clean product experience

Status: Proposed

## Decision summary

AgentMesh will be simple by default and deep by choice. A new user sees a small product built around
Company, Employee, Goal, Approval, and Result. Scenario Packs add useful domain experiences without
exposing the control-plane resources used to implement them. Operators and developers can enter
Advanced mode or the Admin Console when they need exact control.

The virtual Office remains the recognizable company home, but it is an overview and collaboration
surface rather than a container for every setting. Focused work happens in clean scenario
workspaces. Technical detail is available through progressive disclosure, not permanently shown.

This is a product contract. Adding a backend capability does not authorize adding another item to
the default navigation, another dashboard card, or another required setup field.

## Product layers

### Layer 1 - simple core

The core experience answers five questions:

1. What company am I operating?
2. Who are my employees and what can they do?
3. What goal have I assigned?
4. What needs my decision?
5. What result did the company produce?

The corresponding default capabilities are:

- create or install one Company;
- create, appoint, and inspect Employees;
- submit a Goal in natural product language;
- select or confirm an explainable team;
- execute bounded coordinated work;
- display meaningful status and handoffs;
- request human Approval when required;
- deliver versioned Results and Artifacts;
- retain basic governed work history and Memory;
- expose a concise failure or readiness explanation.

These capabilities share the same authoritative Task, Run, Event, Artifact, Policy, and audit
records as Advanced mode. Simplicity is a projection over the real control plane, not a separate
toy implementation.

### Layer 2 - scenario Packs

A scenario Pack contributes domain language and a focused workspace:

- Company Template, departments, Positions, and employee blueprints;
- one or more domain Goal forms;
- business objects and Result renderers;
- bounded workflows, evaluation criteria, and Policy defaults;
- required logical Tool capabilities;
- optional Office rooms and presentation assets.

Installing Music Studio should add a Music Project entry point and music workspace. It should not
add raw generation jobs, MCP servers, audio-analysis internals, or provider callbacks to the main
navigation.

### Layer 3 - optional capability extensions

Extensions are installed or enabled only when a selected scenario needs them:

- live MCP Tool connectors;
- remote A2A peers;
- external Memory backends;
- audio, image, browser, accounting, CRM, and distribution providers;
- custom Controllers and resource types;
- advanced observability and external tracing;
- commercial, regulated, or high-risk Policy Packs.

An extension declares dependencies, permissions, credentials, cost class, data scope, and failure
behavior. The Template presents one understandable connection request, such as "Connect a music
generator," rather than asking the owner to configure implementation components individually.

### Layer 4 - control plane

Advanced mode and the Admin Console expose Agent Definitions and Versions, Task and Run internals,
MCP/A2A registries, Credential Bindings, Policies, Feature Gates, quotas, events, Controller state,
and raw audit evidence. This layer is essential but not part of ordinary daily work.

## Capability activation

Feature Gates remain an operator mechanism. Users choose outcomes, Templates, and connections.
AgentMesh resolves the minimal dependency closure and shows it before activation.

```text
Owner chooses Music Studio Demo
  -> core company + employee + goal + result capabilities
  -> deterministic music fixture
  -> no external connector

Owner enables live generation
  -> music_live_generation
  -> one compatible provider adapter
  -> one Credential Binding
  -> provider budget and Policy
```

The resolver must:

- enable only dependencies declared by the chosen Template or capability;
- reject incompatible or missing versions before changing state;
- preview new permissions, credentials, data egress, and expected cost;
- apply changes transactionally or report a recoverable partial external state;
- preserve core operation when an optional extension is disabled;
- translate readiness failures into product language with an Advanced details link.

There is no default "enable everything" profile. Disabling an optional capability hides its
ordinary UI contribution but never deletes historical evidence.

## Installation profiles

### Demo

- one command or Compose profile;
- deterministic built-in providers and fixtures;
- no API key or external network dependency;
- one complete Company -> Goal -> Approval -> Result loop;
- advanced infrastructure stays hidden.

### Standard

- one guided provider connection at a time;
- a Template requests only the Tools it needs;
- secure defaults for limits, approvals, retention, and data access;
- normal work remains in the Office and scenario workspace.

### Advanced

- explicit infrastructure and policy configuration;
- custom Agents, Packs, Controllers, providers, routing, and evaluations;
- complete audit and operational surfaces;
- no reduction of the security boundaries used by Standard mode.

## Information architecture

The ordinary product shell contains no more than five primary destinations:

| Destination | Owner question |
| --- | --- |
| Company | What is happening now? |
| Goals | What have I asked the company to do? |
| Employees | Who works here and what can they reliably do? |
| Approvals | What needs my decision? |
| Results | What has the company delivered? |

Scenario workspaces open from a Goal or Result, not as permanent global navigation unless the
owner pins them. Settings contains Connections and Preferences. A distinct Admin Console link is
visible to authorized operators but visually separated from daily navigation.

The same object has one primary name. Ordinary surfaces say Employee, Goal, Approval, and Result;
they do not alternate between Agent Definition, Task, policy decision, and Artifact unless the user
opens technical details.

## Office and focused workspace

The Office home provides:

- a calm company overview;
- current Goals and exceptions;
- employees in their departments;
- truthful handoffs and work state;
- one primary "Create goal" action;
- direct entry into the current Result or required Approval.

The Office does not show permanent debug panels, raw event streams, configuration forms, or every
ambient label. Selecting a Goal opens a focused workspace over or beside the world. The workspace
owns domain-specific creation, review, comparison, and approval UI. Closing it returns to the same
Office camera and selection state.

World animation must remain restrained. Ambient movement is slower and visually quieter than real
work handoffs. Motion can be reduced or disabled. A list view provides equivalent functionality and
accessibility; the world is a presentation, not the only control path.

## Visual direction

The desired character is simple, clean, calm, and precise. "Technological" comes from responsive
state, spatial continuity, and trustworthy evidence, not neon colors or dense telemetry.

### Layout

- use generous spacing and a stable grid;
- keep one primary action per surface;
- place secondary actions in contextual menus or a side panel;
- use progressive drawers for details instead of nested modal dialogs;
- keep content widths readable and align related values;
- preserve user location and selection when switching views.

### Color

- use neutral backgrounds and surfaces as the majority of the interface;
- choose one restrained brand accent for selection and primary actions;
- reserve semantic colors for success, warning, error, and waiting states;
- do not assign a saturated color to every department or Agent;
- meet accessible contrast in light and dark themes.

### Typography and iconography

- use one UI type family with a clear size and weight hierarchy;
- use short product labels and plain-language status;
- use a consistent outline icon family;
- avoid decorative badges, gradients, glow, and uppercase labels without meaning;
- distinguish employee, department, and task labels by hierarchy, not competing colors.

### Surfaces and motion

- use subtle borders, limited shadow, and consistent corner radii;
- animate state transitions only when they help explain causality;
- keep loading, queued, working, waiting, and failed states visually distinct;
- avoid continuous motion behind focused reading or approval work;
- support reduced motion, keyboard navigation, and responsive layouts.

## Status language

User-facing status is small and actionable:

| Product status | Meaning |
| --- | --- |
| Preparing | AgentMesh is checking the team and required connections |
| Working | At least one employee owns active work |
| Waiting for you | A decision, permission, or missing input blocks progress |
| Needs attention | Work failed or a configured bound was reached |
| Complete | A durable Result is ready |

Raw scheduler and provider states remain in technical details. Empty states tell the user what to do
next. Error messages name the failed outcome, what remains safe, and the smallest recovery action.

## Music Studio application

The Music Studio first-value path contains four owner steps:

1. describe the song;
2. confirm the proposed team and limits;
3. watch progress and audition candidates;
4. approve or request a specific revision.

The default screen shows the brief, current phase, team, round/budget, candidate player, concise
review, and one next action. It hides provider job IDs, audio metrics, prompt payloads, Run graphs,
and rights evidence behind labelled details. Live generation adds a single connection step. Demo
requires none.

## Extension UI contract

A Pack may contribute a Goal form, Result renderer, focused workspace, employee presentation data,
and semantic Office rooms. Contributions use core design tokens and components. They may not:

- replace global navigation or authentication;
- inject unscoped CSS or executable code into the trusted API process;
- add top-level navigation without owner pinning or an explicit platform decision;
- conceal cost, data egress, Policy, or Approval boundaries;
- show a simulated state as authoritative work evidence;
- require Advanced mode for the first successful scenario Goal.

The host owns accessibility, localization, loading/error states, permission checks, and responsive
behavior. Packs own domain content and may supply bounded visual assets.

## Delivery plan

### Slice 0 - product contract

- [ ] define the core-versus-extension capability catalog;
- [ ] define dependency preview and activation contracts;
- [ ] adopt the five-item primary information architecture;
- [ ] publish shared visual tokens, status language, and UI contribution rules;
- [ ] add simplicity checks to Pack review.

### Slice 1 - clean shell

- [ ] simplify the Office chrome and retain one primary action;
- [ ] move debug/configuration content to the Admin Console or details drawers;
- [ ] add Goals, Employees, Approvals, and Results projections;
- [ ] provide equivalent world and accessible list navigation;
- [ ] implement light, dark, reduced-motion, and responsive baselines.

### Slice 2 - guided activation

- [ ] install a Demo Company without credentials;
- [ ] resolve Template dependencies automatically;
- [ ] add a plain-language connection wizard and readiness summary;
- [ ] support reversible optional capability activation;
- [ ] preserve evidence when a capability is disabled.

### Slice 3 - extension workspace

- [ ] define sandboxed Pack workspace contributions;
- [ ] ship the Music Studio focused workspace as the first implementation;
- [ ] test an unrelated Pack against the same components and navigation rules;
- [ ] publish screenshots and visual-regression fixtures.

## Acceptance criteria

- A new user can finish Demo without seeing MCP, A2A, Feature Gate, Task, Run, or Credential terms.
- The ordinary shell has at most five primary destinations and one primary action per surface.
- A scenario asks only for connections required by the chosen live capability.
- Core Company, Employee, Goal, Approval, and Result flows work with every optional extension off.
- Disabling an extension does not break core history, audit, or unrelated scenarios.
- Every simplified state links to authoritative evidence in Advanced mode.
- Music Studio Demo is understandable without documentation or an API key.
- World and list views provide equivalent goal, employee, approval, and result access.
- Core and Pack UI pass localization, keyboard, contrast, reduced-motion, and responsive checks.
- Visual-regression tests cover the primary empty, working, waiting, failed, and complete states.

## Non-goals

- removing the control plane or weakening auditability to create a simpler appearance;
- exposing every installed capability in global navigation;
- treating the game world as the only usable interface;
- using animation, color, or gamification as a substitute for real state;
- requiring users to understand infrastructure before receiving a first Result;
- forcing every scenario to use the 3D renderer;
- creating a separate simplified backend with incompatible data or semantics.

## Relationship to other proposals

- [Employee-first virtual company](employee-first-virtual-company-and-extension-platform.md)
  defines the five owner-facing concepts and durable employee model.
- [AgentMesh Music Studio](music-studio-template.md) is the first focused scenario workspace.
- [Office game-world evolution](agentmesh-office-game-world.md) provides the spatial presentation.
- [Office 2.5D renderer](agentmesh-office-2.5d-renderer.md) remains an optional renderer, not a
  prerequisite for core operation.
