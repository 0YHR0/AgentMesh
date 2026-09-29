# Control Plane P0 — remaining implementation plan

Status: Active plan · Updated: 2026-09-29 · Parent: [#134](https://github.com/0YHR0/AgentMesh/issues/134)

The framework-neutral Runtime SDK, LangGraph/subprocess conformance, and managed Direct/Reviewed/
Coordinated qualification slices are present. The default Worker is still the supported LangGraph
path. A4.2d is `qualified_with_documented_exceptions`; its
[parity report](../qualification/a4-2-parity.json) records safety differences instead of claiming
exact equivalence. No managed cutover gate should become a production default on this evidence
alone. See [implementation status](../implementation-status.md).

## Remaining tracks

| Track | Required outcome | Acceptance evidence |
| --- | --- | --- |
| [#136](https://github.com/0YHR0/AgentMesh/issues/136) | A non-LangGraph subprocess can restart/reattach durably without duplicate provider work; admission and authority remain fenced | Process-death/restart, replay, cancellation, migration and rollback tests against real PostgreSQL |
| [#137](https://github.com/0YHR0/AgentMesh/issues/137) | One Governed Action SDK protects a non-MCP external effect with the same Intent/Permit/evidence rules | Fake effect, exact approval, duplicate/unknown outcome, audit, and negative authorization tests |
| [#138](https://github.com/0YHR0/AgentMesh/issues/138) | Repeatable chaos smoke and a machine-readable correctness baseline | One-command disposable-environment run, invariant oracle, JSON and human reports, and documented limitations |

The target contracts are [Managed Agent Runtime](modules/formal/managed-agent-runtime.md),
[Governed Action Protocol](modules/formal/governed-action-protocol.md), and
[Reliability/Chaos](modules/formal/reliability-model-and-chaos.md). ADR 0007 defines
[control-plane ownership](../adr/0007-framework-neutral-agent-control-plane.md).

## Implementation order and safety

1. Finish durable runtime reattach and recovery under default-off gates. A Run keeps its pinned
   Runtime Version and Assignment digest; a replacement Attempt must inspect or reconcile known
   provider execution before dispatching again.
2. Add a fake non-MCP governed action before connecting a real external writer. The Runtime never
   receives raw Permit authority, and timeout/unknown cannot be treated as permission to retry.
3. Run crash-window qualification on disposable infrastructure. Publish exact test inputs,
   failures, invariant results, and exceptions before proposing any default cutover.

Each slice needs an isolated PR with its authority change, crash windows, migration/rollback
plan, gate-off behavior, conformance tests, real PostgreSQL tests, and CI result. Schema expansion,
default cutover, and legacy removal must not be combined in one PR. The operator
[rollback runbook](../operations/runtime-direct-cutover-rollback.md) must remain valid.

Stop and revise the design if a Runtime needs direct access to Task/Run/Attempt or Policy tables,
if an external call would run inside a database transaction, if a stale Attempt could overwrite
current authority, or if an unknown side effect would be retried without proof of non-delivery.

## Exit demonstration

In one deployment, a LangGraph Agent and an independent non-LangGraph Agent must produce governed
Task/Run/Attempt evidence. A fake external write must require approval and execute once. Killing
the Worker in a dispatch window must recover or stop with an explicit unknown state, never a
duplicate effect. The chaos report must state which invariants passed and which remain unqualified.
