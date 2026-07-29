# Office primary surface and expandable campus

Status: Implemented baseline; shared-layout follow-up implemented

## Problem

The first 2.5D Office could observe work but sent operators to the Control Console to create it.
Its four hard-coded departments also made the company metaphor look smaller than the Agent
Registry and prevented an operator from shaping their own workspace.

## Decision

Treat `/world-3d` as the daily company interface and `/` as the administration interface.

The Office owns two bounded operator workflows:

- create a real direct or coordinated Task through the existing Control API, with optional
  immediate execution;
- add personal campus spaces through a local layout planner.

Task state remains authoritative server data. Campus layout is presentation preference only.
The first implementation stored at most eight custom spaces in browser local storage. The
shared-layout follow-up persists their bounded presentation definitions in PostgreSQL under the
tenant Office layout, while retaining a one-time import path for existing browser-local layouts.

## Default campus

The default campus contains Product, Research, Analysis, Security, Design, Engineering,
Operations, and People spaces. Each has a different facility silhouette and equipment set.

Custom spaces select one of the existing visual grammars, provide a name and accent color, and
occupy the next deterministic grid slot. Campus bounds, roads, camera clamps, labels, and the
navigation map derive from the resulting space collection.

Agent placement considers role, capability, tags, and custom-space name keywords. Task and Agent
records are never modified by layout changes.

## Boundaries

- `/` remains available for Agent lifecycle, Tool, Approval, Artifact, identity, and advanced Task
  administration.
- Task creation uses `POST /api/v1/tasks`; start-now uses the existing idempotent Run command.
- Feature Gates still control coordinated execution and the 2.5D route.
- Custom-space definitions are tenant-shared through authorized Office layout APIs. Their rendered
  geometry remains outside the authoritative employee-placement grid.
- The lightweight `/world` renderer remains available.

## Acceptance criteria

- a Task can be created without leaving `/world-3d`;
- coordinated role inputs are available only when the capability is enabled;
- the default campus has eight independently navigable spaces;
- adding a custom space expands scene bounds and survives reload;
- no layout operation mutates Task, Run, Agent, or Handoff state;
- the Admin Console is clearly named and links back to the primary Office.
