# Music Studio implementation priority

Status: Active delivery plan
Last updated: 2026-08-02

## Delivery rule

AgentMesh will deliver one useful vertical product before expanding optional infrastructure. The
first product is complete only when an owner can create a music goal, observe specialist handoffs,
audition a generated candidate, approve it, and download a durable release package.

Work is ordered by user outcome, not by completing every formal platform module.

## P0 - end-to-end local product

P0 uses real AgentMesh persistence, coordination, Artifacts, evidence, and UI. Paid or unstable
external services are replaced by deterministic local adapters.

### P0-A - installable Music Studio contract

- [x] declarative Company Pack;
- [x] minimal departments and employee Positions;
- [x] versioned Music Project, lyrics, candidate, review, and release object schemas;
- [x] preview and one-transaction installation APIs;
- [x] deterministic contract and API tests.

### P0-B - deterministic music project workflow

- [x] add valid WAV Artifact support with container validation and correct download metadata;
- [x] add a reproducible local music provider and analysis of the actual generated audio bytes;
- [x] create a Music Project through one product API;
- [x] create and appoint a fixed coordinated specialist plan;
- [x] store audio and documents as immutable Artifacts;
- [x] create an evidence-backed listening review and shortlist decision;
- [x] add the owner-requested bounded revision path;
- [x] materialize and approve a final release package without an external API key.

### P0-C - clean focused workspace

- [x] create a project from the Office;
- [x] show brief, team, phase, handoffs, round, and blockers;
- [x] play and compare candidate audio;
- [x] show one concise review and one next action;
- [x] approve or request a bounded revision;
- [x] download the final package;
- [x] keep technical details in the Admin Console.

### P0-D - product qualification

- [ ] one-command Demo startup;
- [x] first-run empty state and guided installation;
- [x] English and Chinese UI;
- [ ] deterministic API, workflow, browser, restart, and Compose E2E tests;
- [ ] README quickstart using no credentials.

P0 exit criterion: a new user completes the full Demo from the primary UI without documentation,
an API key, or exposure to MCP, A2A, Task, Run, and Feature Gate terminology.

## P1 - live useful production

- Suno adapter behind the provider-neutral generation contract;
- Credential Binding and guided connection readiness;
- asynchronous submit, poll, import, retry, and unknown-outcome recovery;
- deterministic audio signal analysis plus an optional audio-capable review model;
- authorized trend evidence connector;
- real budget and provider-credit reporting;
- terms, plan, input authorization, and provenance snapshot.

P1 does not include distribution. The owner downloads an approved result.

## P2 - persistent employee development

- reviewed genre, language, and project Memory;
- employee profiles and work history in the simple UI;
- evidence-backed qualifications and critic calibration;
- explainable team composition beyond the starter team;
- controlled strategy comparisons and immutable Agent Version proposals;
- optional external Memory adapters selected by the owner.

P2 never grants authority from Memory or performance automatically.

## P3 - optional studio expansion

- stems, MIDI, mastering, cover art, subtitles, and portfolio management;
- additional generation and audio-analysis providers;
- custom departments, workflows, scorecards, and Controller extensions;
- remote A2A specialists;
- governed distribution and commercial Policy Packs;
- multi-company, high-availability, and scale-oriented infrastructure.

## Explicitly deferred from P0

- live trend scraping;
- Suno credentials or paid provider usage;
- external Memory services;
- free-form team composition;
- autonomous employee training;
- voice cloning or identifiable artist imitation;
- automatic publishing, distribution, monetization, or contracting;
- complex financial, CRM, accounting, and cross-tenant behavior;
- mandatory 3D rendering.

## Progress reporting

Every implementation PR updates this file and `implementation-status.md`. Progress is reported by
completed user-visible exit criteria, not by file count or interface scaffolding.
