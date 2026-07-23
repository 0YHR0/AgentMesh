# v1 SLO and restore runbook

Status: Supported single-team baseline
Last updated: 2026-07-23

## Service objectives

Measured over a rolling 30-day window, excluding declared maintenance:

- Control API availability: 99.5%.
- Accepted Task creation latency: p95 below 750 ms.
- Outbox publication lag: p95 below 10 seconds; poison rows are quarantined within 5 attempts.
- Ready Run dispatch latency: p95 below 30 seconds when capacity is available.
- Domain-event Console freshness: p95 below 15 seconds, including polling fallback.
- PostgreSQL recovery point objective: 24 hours for the bundled local backup procedure.
- Recovery time objective: 60 minutes for the documented single-node restore drill.

Production operators should tighten these only after representative load tests. Error-budget
exhaustion pauses non-essential releases; security revocations and data-integrity fixes remain
allowed.

## Backup

Run from the repository root while PostgreSQL is healthy:

```bash
python scripts/operations/backup.py
```

The output contains a custom-format PostgreSQL dump, the content-addressed Artifact directory, and
a SHA-256 manifest. Redis is deliberately excluded because it is transport state, not the business
source of truth.

## Restore drill

Use an isolated Compose project or stop API, Worker, Relay, and Reconciler before restoring:

```bash
python scripts/operations/restore.py backups/agentmesh-YYYYMMDDTHHMMSSZ --yes
docker compose up -d
python scripts/ci/compose_e2e.py
```

Verify `/ready`, Task→Run→Attempt lineage, one Artifact digest/download, replay bookmarks, pending
approvals, Outbox backlog, and a new end-to-end Task. Record the manifest digest, duration, row
counts, and operator identity. A backup is not considered usable until this drill succeeds.

## Incident triggers

- API readiness failure for 5 minutes.
- Outbox pending/quarantined growth for 10 minutes.
- Attempt lease-expiry spike or zero healthy Agent capacity.
- Artifact digest mismatch.
- MCP circuit open repeatedly after cooldown.
- A2A correlation in outcome-unknown state beyond its reconciliation window.

Artifact digest mismatch, credential exposure, or suspected MCP/A2A compromise requires immediate
traffic isolation and credential/Version revocation before reconciliation.
