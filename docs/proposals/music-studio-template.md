# Live Music Studio integrations

Status: Proposed · Last updated: 2026-09-29

The current keyless Music Studio demo uses deterministic local audio and lets an owner compare
candidates, request bounded revisions, approve a result, and download a durable release package.
It does **not** call Suno, search live music trends, or autonomously publish or monetize a song.
The [implementation status](../implementation-status.md) describes the platform boundary.

## Proposed next outcome

An owner configures approved model and music-generation connections, supplies a creative brief,
and starts a bounded project from a focused Console workspace. Specialist employees research
authorized evidence, write and refine lyrics, generate candidates, inspect actual audio, and
present a result for the owner's decision. Every external job, revision, review, cost, and output
remains attributable to a Task, Agent Version, and Artifact.

## Required work

1. Define a provider-neutral generation contract and certify one real provider adapter. Confirm
   its terms, credential handling, job lifecycle, output format, and retry/idempotency behavior.
2. Add explicit connection preflight, bounded submit/poll/import, and unknown-outcome recovery.
   Provider credentials must stay outside Task content and employee instructions.
3. Verify actual audio bytes with deterministic signal analysis. An optional audio-capable model
   may review the recording, but a text-only model must not claim to have listened.
4. Bind trend research to authorized evidence tools and cite collected sources; do not treat a
   model's unaided popularity claims as market data.
5. Record provider credits/costs, candidate provenance, lyric and audio rights metadata, and
   the owner's revision/approval decisions before producing the final package.
6. Expose only the brief, team, current phase, candidates, concise review, budget, and next action
   in the primary workspace; keep raw tool/job evidence one level deeper.

## Acceptance boundary

- The existing offline demo keeps working without a key or network access.
- A real run can survive Worker restart without duplicating a confirmed provider job or hiding an
  unknown external outcome.
- A candidate cannot be described as “listened to” without audio-derived evidence.
- Revisions are bounded by explicit criteria, deadline, and budget; exhaustion asks the owner.
- The owner approves the selected package. Automatic distribution, payments, voice cloning, and
  imitation of identifiable artists are outside this proposal.
