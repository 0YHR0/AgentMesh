# Deterministic direct-runtime admission rollback

Last updated: 2026-08-20

This runbook describes the A4.1a test-only admission gate. It is not a production runtime
cutover procedure.

## Scope

`managed_runtime_direct_cutover` is disabled in all default profiles and must remain disabled on
the test server until the later execution slice is explicitly qualified. When enabled, bootstrap
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
3. Do not rewrite existing `runtime_authority=managed` rows or change their Runtime Version. This
   A4.1a slice has no Worker cutover, so those rows must not be claimed as evidence of a completed
   managed execution path.
4. Inspect the persisted Run and outbox records before any later execution slice is enabled.
   Reconciliation or data repair is a separate, explicitly approved operation; never silently
   retry by creating a new ordinary Run.

Migration 0047 is expand-only and keeps legacy rows valid. A schema rollback is performed through
the repository's tested Alembic downgrade window, not by manually dropping the managed admission
columns or constraints.
