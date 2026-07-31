# AgentMesh Music Studio company template

Status: Proposed

## Decision summary

AgentMesh Music Studio will be the first user-facing scenario Pack built around the
employee-first product promise. A company owner enters a creative goal, selects a music team,
watches its employees research, write, produce, listen, revise, and hand work to one another, then
approves a durable final release package.

The scenario must prove more than a sequence of prompts. It uses AgentMesh for durable employee
identity, governed Memory, explainable team composition, bounded iteration, asynchronous Tool
execution, Artifact lineage, human Approval, and visible collaboration in the Office.

Music generation is provider-neutral. The first supported adapter may call Suno, but the Pack
depends on logical music capabilities rather than Suno-specific resources. A provider can be
replaced without changing the company, employees, project history, or review contract.

Market Intelligence remains the implemented engineering reference for evidence-backed coordinated
execution. Music Studio becomes the first scenario designed as the primary user product and the
first candidate for an independently distributed scenario repository.

## Product outcome

```text
Create a Music Project
  -> describe audience, mood, language, genre, references, and constraints
  -> review the proposed employees, limits, and budget
  -> watch research, songwriting, production, and listening handoffs
  -> audition versioned candidates
  -> approve one, request a change, or let the bounded review loop continue
  -> receive final audio, lyrics, production notes, and provenance
```

The output is a reviewable creative work, not a promise that it will become popular, generate
revenue, qualify for copyright protection, or be accepted by a distributor.

## Why music is the first user scenario

- Employees have recognizable specialties and persistent development history.
- Work naturally crosses departments and produces visible handoffs.
- Generation is asynchronous, costly, fallible, and requires governed Tool execution.
- Candidates can be compared without overwriting earlier evidence.
- Critique and revision require a bounded multi-Agent loop, not one unstructured prompt.
- Audio, lyrics, reviews, decisions, and rights information form a useful Artifact graph.
- The owner has an obvious role in auditioning and approving the final creative choice.

It also exposes hard platform requirements early: large binaries, external jobs, provider credits,
uncertain outcomes, multimodal evaluation, originality constraints, and human creative control.

## Default company

```text
Company: AgentMesh Music Studio
Mission: Turn an owner's creative intent into original, reviewed, traceable music.
Default project: One song, one language, one primary audience
Default risk posture: No external publication, distribution, or voice imitation
Default review limit: Three generation rounds, configurable before launch
```

The organization is a starting structure, not a mandatory hierarchy. The owner may add departments,
Positions, and qualified employees through the common AgentMesh model.

## Departments and employees

### Creative Direction

**Creative Director**

- turns owner intent into measurable creative acceptance criteria;
- resolves conflicts between trend relevance, originality, lyrics, and production quality;
- chooses revise, shortlist, escalate, or recommend final approval;
- cannot silently approve the final release on behalf of the owner.

### A&R and Trend Lab

**Trend Researcher**

- studies authorized charts, metadata, audience signals, audio features, themes, and structures;
- separates current evidence from model knowledge and inference;
- produces abstract creative attributes rather than copying a song or artist identity;
- records source scope, observation date, license, and confidence.

### Songwriting Room

**Lyricist**

- writes original lyrics within the brief's language, theme, structure, and safety constraints;
- revises affected sections when critique is specific;
- records lyric versions and the reason for every material revision;
- rejects requests to reproduce lyrics that the owner is not authorized to use.

### Production Studio

**Music Producer**

- turns the brief and lyrics into a provider-neutral Composition Spec;
- selects arrangement, instrumentation, tempo range, vocal character, dynamics, and song form;
- creates variants that test explicit hypotheses;
- does not request exact living-artist imitation or unauthorized voice cloning.

**Generation Operator**

- invokes a configured provider through governed music Tools;
- tracks provider jobs, credits, retries, and unknown outcomes;
- imports completed audio as immutable, digest-addressed Artifacts;
- never exposes provider credentials to another employee.

### Listening Room

**Audio Critic**

- evaluates actual stored audio, not its filename or generation prompt alone;
- combines deterministic analysis with an authorized audio model when configured;
- scores the candidate against the brief and cites the exact Artifact version;
- gives bounded revision advice and records uncertainty.

### Rights Desk

**Provenance Reviewer**

- verifies input authorization, provider plan metadata, and the declared use plan;
- distinguishes composition, lyrics, sound recording, and optional visual assets;
- checks human approval and contribution records before release;
- reports risks and missing evidence without presenting legal advice.

For a minimal deployment, the owner may perform Creative Director and Provenance Reviewer duties.
The other responsibilities remain distinct in workflow evidence even when one runtime fills several
roles.

## MusicProject resource

The Pack contributes a `MusicProject` custom resource. Its desired Spec is owner-controlled and its
Status is reconciled by the Pack Controller or declarative workflow runtime.

```yaml
apiVersion: music.agentmesh.dev/v1alpha1
kind: MusicProject
metadata:
  name: summer-night-single
spec:
  companyRef: company-demo
  brief:
    purpose: streaming single demo
    audience: young adults
    language: zh-CN
    themes: [summer, reunion, city night]
    mood: warm and energetic
    genreAttributes: [dance-pop, bright synths, concise chorus]
    durationSeconds: { min: 150, max: 210 }
    originalityConstraints:
      prohibitArtistImitation: true
  usePlan: internal-demo
  teamPolicy: explainable-auto-compose
  generation:
    providerCapability: music.generate
    candidatesPerRound: 3
    maxRounds: 3
    creditBudget: 12
  acceptance:
    minimumOverallScore: 80
    minimumDimensionScore: 65
    ownerApprovalRequired: true
status:
  phase: ListeningReview
  currentRound: 1
  conditions: []
  candidateRefs: []
  recommendedCandidateRef: null
  finalReleaseRef: null
```

Provider names, API keys, raw prompts, and mutable job details do not belong in the desired Spec.
Credential Bindings and provider policy resolve them at execution time.

## Durable objects and Artifacts

| Object | Purpose |
| --- | --- |
| `CreativeBrief` | Intent, audience, constraints, and acceptance criteria |
| `TrendDossier` | Dated evidence, abstract attributes, confidence, and source lineage |
| `LyricsDraft` | Versioned lyrics, structure, inputs, and revision rationale |
| `CompositionSpec` | Provider-neutral arrangement and generation intent |
| `GenerationJob` | Idempotent external job state, cost, and outcome correlation |
| `AudioCandidate` | Immutable audio Artifact plus provider and input lineage |
| `ListeningReview` | Evidence-backed dimension scores and actionable findings |
| `RevisionDecision` | Accept, revise, reject, or escalate decision |
| `FinalReleasePackage` | Selected deliverables and traceability manifest |
| `RightsManifest` | Input declarations, provider terms snapshot, use plan, and approvals |

Audio bytes live in the Artifact store. PostgreSQL stores metadata, digests, lineage, status, and
authorization. Large binaries are not embedded in Task messages, A2A payloads, Memory, or JSON.

## Coordinated workflow

```text
Owner Creative Goal
  -> Creative Director: normalize brief and acceptance criteria
  -> Trend Researcher: produce Trend Dossier
  -> Lyricist + Music Producer: draft Lyrics and Composition Spec
  -> Provenance preflight
  -> Generation Operator: request N candidates asynchronously
  -> import immutable Audio Candidates
  -> audio analysis + Audio Critic review for every candidate
  -> Creative Director decision
       -> shortlist -> owner audition and Approval
       -> targeted revision -> next bounded round
       -> blocked/risky -> owner decision
  -> Final Release Package
```

Each arrow is a persisted handoff event with source employee, destination employee, input Artifact
references, reason, and correlation identifiers. A2A may transport delegation, but AgentMesh Task,
Run, Artifact, and event records remain authoritative.

Independent work runs concurrently. Deterministic audio analysis can execute for candidate B while
an audio model reviews candidate A. Lyrics and production planning may run in parallel only after
both accept the same immutable Creative Brief version.

## Bounded satisfaction loop

"Until satisfied" is a product intent, not an unbounded runtime instruction. Every project defines:

- maximum rounds and candidates per round;
- provider-credit budget and wall-clock deadline;
- stage timeouts, minimum scores, and hard failure dimensions;
- who may request revision and who may approve release;
- escalation behavior when evidence is missing or reviewers disagree.

The decision policy rejects hard-policy violations, shortlists candidates meeting every threshold,
and revises only when the finding names a bounded change and budget remains. Tied or uncertain
creative judgment moves to `WaitingForOwner`. Exhausted limits produce `NeedsDirection`, never an
infinite retry. Owner revision preserves the rejected candidates and prior reviews.

## Listening and evaluation contract

An Audio Critic may claim to have listened only when its Run records successful access to the
candidate audio Artifact or derived evidence produced from that Artifact.

The baseline listening bundle contains encoding validation; tempo and beat confidence; estimated
key; loudness, peak, clipping, dynamics, and silence measures; section estimates; optional
transcription and lyrics alignment; and optional audio-model observations with provenance.

The scorecard covers brief fit, structure, melodic coherence, vocal/lyric fit, arrangement, mix
intelligibility, emotional effect, originality risk, and technical defects. Each dimension includes
a score, confidence, evidence reference, and proposed action.

Deterministic measures do not pretend to judge taste. Model observations do not masquerade as
signal measurements. The Creative Director combines both and explains disagreement.

## Provider-neutral capabilities

| Capability | Effect | Required behavior |
| --- | --- | --- |
| `music.trends.read` | read | Authorized, dated evidence with source provenance |
| `music.audio.analyze` | compute | Analyze an immutable Artifact into derived evidence |
| `music.generate` | external write | Idempotent submission with cost and job correlation |
| `music.generation.read` | read | Poll or retrieve provider job state |
| `music.stems.export` | external write | Optional export with capability discovery |
| `music.audio.import` | write | Verify and store output as an Artifact |

The Suno adapter is one implementation. It maps Composition Spec and Lyrics Draft inputs, handles
asynchronous completion, imports output, and records the provider plan and terms snapshot.
Unsupported capabilities become readiness Conditions rather than simulated successes.

Every provider adapter supports Credential Broker references, stable operation keys,
duplicate-submission protection, explicit outcome-unknown state, limits, safe import validation,
redacted diagnostics, and a deterministic fake provider for CI.

## Research, originality, and rights boundaries

Trend research uses authorized sources. It may retain factual metadata, licensed data, bounded
evidence references, derived audio features, and high-level theme or structure summaries. The
default Pack does not ingest or reproduce complete third-party lyrics, melodies, recordings, or
paywalled catalogs.

The production contract prohibits exact copying, living-artist names as operative specifications,
unauthorized voice cloning, claims of zero third-party risk, and claims of commercial rights merely
because generation succeeded.

Rights depend on inputs, provider plan and terms, territory, human contribution, and intended use.
The Rights Manifest records those facts and open questions but makes no legal conclusion. External
distribution and monetization remain disabled until separately governed connectors exist.

## Final release package

- selected WAV or highest-quality audio and a playback derivative;
- final lyrics with version history;
- Creative Brief and Composition Spec;
- candidate comparison and final Listening Review;
- generation and Artifact lineage;
- Rights Manifest and provider-plan snapshot;
- human decisions and contribution log;
- optional stems, MIDI, cover art, or subtitles when supported and approved.

The package is immutable after approval. Remasters, lyrics changes, and alternate versions create
new release revisions linked to the original.

## Employee continuity and development

Employees keep identity across projects while instructions, models, Tools, and qualifications are
versioned. Evidence may propose genre Memory for a researcher, a language qualification for a
lyricist, a tested generation strategy for a producer, or critic calibration against owner choices.

There is no fictional XP. Quality claims show scope, sample size, evaluator, and Artifact evidence.
Memory and performance never self-grant a Tool, budget, Position, or Approval authority.

## Office experience

The Pack contributes Creative Direction, A&R and Trend Lab, Songwriting Room, Production Studio,
Listening Room, Rights Desk, and Release Vault. The owner creates a Music Project in the Office.
Employees move only for persisted assignments and handoffs. Generation displays durable external
job state, not fake activity. The Listening Room provides playback, A/B comparison, scorecards,
revision targets, and Approval; advanced evidence links to the Admin Console.

The world remains a calm company overview. Selecting a Music Project opens a clean focused
workspace rather than adding permanent production controls to the map. The default workspace
shows only the brief, current phase, team, round and budget, candidate player, concise review, and
one next action. Provider jobs, raw audio metrics, prompts, Run graphs, and provenance details
remain one level deeper. It follows the shared
[simple core and clean product experience](simple-core-and-clean-product-experience.md).

## Installation modes and Feature Gates

**Demo** uses fake trend and generation adapters plus short audio fixtures. It needs no API key or
network and demonstrates the entire review loop.

**Standard** needs one model provider and one generation-provider Credential Binding. Trend and
audio-model connectors are optional. **Advanced** exposes custom employees, policies, providers,
Memory, departments, rooms, and scorecards.

```text
music_studio
music_live_generation
music_audio_model_review
music_stems_export
music_external_distribution
```

Only `music_studio` is required for Demo. External distribution is out of the first implementation.

## Delivery plan

### Slice 0 - deterministic Pack

- [ ] define schemas, role blueprints, workflow, policies, and Office contributions;
- [ ] implement fake trend, generation, and audio-analysis adapters;
- [ ] run an offline project with bounded revision and final package;
- [ ] publish external-Pack contract fixtures.

### Slice 1 - useful Office experience

- [ ] create a Music Project from the Office;
- [ ] show team proposal, assignments, handoffs, budget, rounds, and blockers;
- [ ] add candidate playback, A/B comparison, reviews, and owner decisions;
- [ ] retain low-level evidence in the Admin Console.

### Slice 2 - live generation

- [ ] certify a Suno adapter against the logical capability contract;
- [ ] implement readiness, submit, poll, import, and unknown-outcome recovery;
- [ ] record provider plan, terms metadata, and cost evidence;
- [ ] qualify restart, duplication, timeout, and malformed-output behavior.

### Slice 3 - real listening

- [ ] implement deterministic audio analysis and derived Artifacts;
- [ ] add an optional audio-capable model adapter;
- [ ] enforce listening evidence and owner-calibration contracts;
- [ ] test conflicting reviews, exhausted budgets, and owner overrides.

### Slice 4 - external Pack

- [ ] move the scenario to `agentmesh/pack-music-studio` using only public contracts;
- [ ] pass offline, PostgreSQL, restart, upgrade, security, and E2E suites;
- [ ] publish compatibility and provider certification matrices;
- [ ] leave only a minimal compatibility fixture in core.

### Slice 5 - optional expansion

- [ ] stems, MIDI, mastering, cover-art, subtitle, and portfolio workflows;
- [ ] additional generation and analysis providers;
- [ ] governed distribution and territory-specific policy Packs.

## Acceptance criteria

- Demo completes without an API key or network access.
- The first project can be created, auditioned, revised, and approved without exposing MCP, A2A,
  Feature Gate, Task, Run, or provider-job terminology.
- Standard setup needs at most one model and one music-provider connection.
- Every candidate has immutable inputs, job, cost, digest, and Artifact lineage.
- A critic cannot claim listening without actual audio-derived evidence.
- Every revision names its failed criterion, target, requested change, and remaining bound.
- Interruption does not duplicate generation or lose known job state.
- Exhausted limits create owner-visible direction instead of another retry.
- The Result includes audio, lyrics, reviews, provenance, and human decisions.
- Employee history survives model, Tool, provider, and instruction changes.
- Office status reflects persisted workflow and Tool events.
- The scenario can run outside core through public extension contracts.
- No default path publishes, monetizes, copies an artist, or clones a voice.

## Non-goals

- guaranteeing popularity, income, or legal protectability;
- replacing human musicians, producers, lawyers, or distributors;
- unlimited autonomous revision;
- storing unauthorized catalogs or complete lyrics;
- exact imitation of artists, songs, performances, or voices;
- training foundation models on customer audio;
- making Suno or any provider part of the AgentMesh core contract.

## External references

- [Suno API Platform](https://platform.suno.com/) describes the current official API offering.
- [Suno ownership guidance](https://help.suno.com/en/articles/2416769) distinguishes
  plan-dependent usage rights.
- [Suno Studio export guidance](https://help.suno.com/en/articles/7940161) documents current
  multitrack, stems, MIDI, and WAV-oriented workflows.
- [US Copyright Office: compositions and recordings](https://www.copyright.gov/register/pa-sr.html)
  explains why the Rights Manifest tracks them separately.
- [US Copyright Office AI report](https://www.copyright.gov/newsnet/2025/1060.html) summarizes its
  current human-authorship analysis for AI-assisted works.

Provider capabilities and terms can change. Adapter certification must verify them and present the
applicable terms rather than treating this proposal as a provider contract or legal opinion.

## Relationship to other proposals

- [Employee-first virtual company](employee-first-virtual-company-and-extension-platform.md)
  defines product language, employee identity, team composition, and the extension boundary.
- [Virtual Company operating model](virtual-company-operating-model.md) defines Company, Position,
  Appointment, Goal, Operation, and Pack resources.
- [Organizational memory service](organizational-memory-service.md) governs durable Memory.
- [Office game-world evolution](agentmesh-office-game-world.md) defines truthful presentation.
- [Market intelligence studio](market-intelligence-studio-template.md) remains the implemented
  evidence-backed engineering reference while this scenario exercises creative multimodal work.
