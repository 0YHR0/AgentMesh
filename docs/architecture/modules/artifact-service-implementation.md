# Artifact Service implementation

Status: Accepted for implementation increment
Owners: AgentMesh maintainers
Depends on: [Artifact Service formal L2](formal/artifact-service.md),
[Cross-module contracts](formal/cross-module-contracts.md)

## 1. Scope

This increment provides the first durable Artifact vertical slice needed before MCP resources,
A2A Parts, and multi-Agent handoffs can exchange stable content references. It intentionally
implements only the `INLINE_SMALL` storage class for UTF-8 `text/plain` and `application/json`.
The default maximum is 64 KiB and can be lowered or raised by deployment configuration.

Large-file direct upload, S3-compatible object storage, malware/DLP scanning, access grants,
retention, relations, and external fetch remain deferred. The API reports
`scan_status=NOT_CONFIGURED`; callers must not interpret availability as malware-clean content.

## 2. Owned entities

| Entity | Implemented responsibility |
|---|---|
| Artifact | tenant-scoped stable identity, owner, display name, kind and classification |
| ArtifactVersion | immutable version number, media type, bytes, size, SHA-256, producer Run and status |
| ArtifactRef | content-free cross-module reference emitted in lifecycle events |

An Artifact row is locked before reserving the next version number. PostgreSQL enforces unique
`artifact_id + version_number`, positive sizes, and equality between declared size and stored
byte length. Version content cannot be updated through the repository.

## 3. Validation and integrity

- content must be non-empty, within `AGENTMESH_ARTIFACT_MAX_INLINE_BYTES`, and valid UTF-8;
- embedded NUL is rejected;
- JSON media requires syntactically valid, non-null JSON;
- callers may provide `expected_sha256`, which must match before persistence;
- the service computes SHA-256 over the exact downloaded bytes;
- arbitrary binary and active media types are rejected by the inline path.

The download response includes `Digest`, a strong SHA-256 `ETag`, a generated safe filename, and
`X-Content-Type-Options: nosniff`. User display names never become storage keys or response
filenames.

## 4. HTTP surface

The Feature-Gated `/api/v1` API supports:

- create an Artifact with its first immutable Version;
- append another immutable Version;
- list and get tenant-scoped Artifact metadata;
- download a Version's exact content.

Create commands accept `Idempotency-Key`. Reusing a key with the same content and metadata returns
the original Artifact; different input returns `idempotency_conflict`.

## 5. Consistency and events

Artifact metadata, inline bytes, idempotency records, and Outbox events commit in one PostgreSQL
transaction. Creation emits `agentmesh.artifact.created` and
`agentmesh.artifact-version.available`; later versions emit another available event. Event
payloads contain ArtifactRef fields and never contain content.

An optional `producer_run_id` is accepted only when the Run belongs to the current tenant. The
database foreign key preserves valid lineage while the Run exists.

## 6. Feature profile

`artifact_service` is disabled in `minimal` and `standard`, and enabled in `full`. Registry
internals and core Task execution do not depend on it. Disabling the Gate hides no data and does
not change migrations; it only blocks the server API boundary with `403 feature_disabled`.

## 7. Console workflow

When `artifact_service` is enabled, the built-in Console provides an Artifact catalog with safe
inline text/JSON creation and append-only Version creation. Metadata views expose classification,
owner, media type, size, storage/scan status, producer Run, timestamps and the complete SHA-256.
Authenticated preview and download fetch content through the existing API, so Bearer authorization,
tenant scoping, integrity headers and media restrictions remain authoritative.

Task detail derives a bounded lineage projection by matching each Artifact Version's
`producer_run_id` to the Task's persisted Runs. It does not infer provenance from filenames or
content. This projection links operators back to the stable Artifact aggregate without adding a
second relation store.

## 8. Deferred target work

- upload sessions and direct multipart upload to S3-compatible private storage;
- Blob content addressing, deduplication and encryption domains;
- scanner/validator jobs, quarantine and rejection lifecycle;
- policy-backed access grants, signed URLs, legal hold and deletion saga;
- Artifact relations, external references, excerpts and bounded manifests;
- Runtime output promotion and automatic replacement of large message payloads.

These remain explicit follow-up increments rather than hidden claims in the inline baseline.

## 9. Verified acceptance criteria

- Version numbers are monotonic and immutable under concurrent row locking.
- Size, media, UTF-8/JSON and optional integrity hashes are enforced before commit.
- Cross-tenant metadata and content access return not found.
- Duplicate creates replay safely while conflicting idempotency keys are rejected.
- Content is absent from Outbox events and API metadata responses.
- Real PostgreSQL/Redis integration binds an Artifact Version to a completed Run, downloads the
  exact bytes, and publishes its lifecycle events.
