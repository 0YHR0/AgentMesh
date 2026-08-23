# Deterministic direct-runtime admission rollback

Last updated: 2026-08-23

This runbook describes the A4.1a/A4.1b CI/test-only admission, managed DIRECT Worker, and
evidence-driven reconciliation path.
It is not a production runtime cutover procedure.

## Scope

`managed_runtime_direct_cutover` is disabled in all default profiles and must remain disabled on
the test server. When enabled, bootstrap
accepts it only in `test`/`testing` with the deterministic model provider and requires
`managed_runtime_worker`.

The gate changes admission for new `DIRECT` Runs only. It never re-evaluates an existing Run:
`runtime_authority`, comparison mode, Runtime Version, and execution-intent identity remain the
immutable persisted snapshot.

## Rollback

1. Stop admitting new Runs (or stop the test API) and set
   `managed_runtime_direct_cutover=false`.
2. Restart the API with the normal profile/configuration. The body-less task-run API remains
   available and new Runs use legacy authority.
3. Do not rewrite existing `runtime_authority=managed` rows or change their Runtime Version.
   Disabling the admission gate affects new Runs only. Keep `managed_runtime_worker` and the
   pinned built-in v2 adapter available until every existing managed Run is terminal or explicitly
   parked for reconciliation; an existing managed Run must never fall back to legacy execution.
4. Inspect RuntimeExecution phase, current owner/fence, latest Attempt, Inbox, and reconciliation
   Outbox evidence. `DISPATCHING` or later with an expired owner and no reattach proof must park as
   `RECONCILIATION_REQUIRED`; it must not be redispatched or replaced by an ordinary Run.
5. Use only the gated, permission-protected runtime outcome reconciliation command to converge a
   `RECONCILIATION_REQUIRED` execution from canonical provider evidence. The command remains
   available when direct-cutover admission is off and never redispatches the provider. Manual
   status edits, direct database repair, and blind provider retry are prohibited.

Migration 0047 keeps legacy rows valid. Migration 0048 is the expand phase for future Runtime
outcome reconciliation readers and storage; this compatibility release does not write its new
values, so a clean 0048-to-0047 downgrade remains supported before writer activation. Once the
writer release stores `RECONCILED` observation evidence or `RECONCILE_RUNTIME_*` TaskResolution actions,
0048 becomes the schema floor. Roll application binaries back to the 0048 compatibility release,
not to an older reader, and never rewrite reconciliation audit evidence to force a downgrade.
