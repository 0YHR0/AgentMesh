# Identity, Principal, and RBAC baseline implementation

Status: Partial

Feature Gate: `identity_rbac` (explicit opt-in; disabled in every built-in profile)

## Responsibility

This increment establishes a fail-closed identity boundary for the single-tenant Control API. It
does not implement the complete formal Identity, tenancy, and secrets module.

When enabled, every `/api/v1` route authenticates an HTTP Bearer credential and resolves an
immutable `PrincipalContext`. Health, readiness, OpenAPI, and documentation remain unauthenticated
so an orchestrator can probe the service and clients can discover the authentication contract.

## Credential configuration

`AGENTMESH_IDENTITY_PRINCIPALS_JSON` is a JSON array of configured Principals. Each entry contains:

- stable `principal_id`, `tenant_id`, `principal_type`, and `status`;
- one or more bounded `roles`;
- a lowercase SHA-256 `token_sha256` digest, never the raw Bearer token;
- optional timezone-aware `expires_at`.

Startup fails when the Gate is enabled with no Principal, malformed data, duplicate IDs or token
digests, unknown roles, or invalid expiry. Raw credentials are compared through constant-time
digest comparison and are never persisted, returned, or logged by AgentMesh.

The configured digest mechanism is a bootstrap/local-deployment adapter. It is not a replacement
for OIDC, workload identity, or managed PAT lifecycle.

## Roles and default-deny permissions

| Role | Intended baseline access |
|---|---|
| `TENANT_ADMIN` | All current permissions in the configured tenant |
| `OPERATOR` | Task create/read/operate/resolve, Artifact use, audit and observability reads |
| `AGENT_AUTHOR` | Agent Registry definitions, draft Versions, and review submission |
| `AGENT_PUBLISHER` | Agent publication/revocation/default selection and Deployment writes |
| `AUDITOR` | Read-only Task, Agent, Artifact, Tool audit, feature and observability access |

Routes authorize stable resource/action permissions rather than trusting client role names.
Permissions absent from the role matrix are denied. Agent management distinguishes reads from
writes, publication/deployment requires `agent:publish`, and Task resolution requires
`task:resolve` instead of general Task read access. `AGENT_AUTHOR` cannot publish its own Version.

The authenticated Principal tenant must match the configured control-plane tenant. This is a
single-tenant guard, not database row-level multi-tenancy.

## Audit identity

When the Gate is enabled, resolution and Handoff APIs replace any client-supplied actor/requester
with `PrincipalContext.principal_id`. When disabled, the prior local-development behavior remains
compatible. `GET /api/v1/identity/me` returns the current safe Principal context and no credential
material.

## Failure behavior

- missing, malformed, unknown, short, inactive, expired, or wrong-tenant credentials return `401`
  with `WWW-Authenticate: Bearer`;
- an authenticated Principal without the required permission returns stable `403`
  `authorization_denied`;
- authentication executes before optional Feature checks on router-scoped APIs, avoiding Feature
  discovery by unauthenticated callers.

## Deliberate boundary

This baseline does not provide persistent Principal/RoleBinding administration, Groups,
delegation, OIDC/SAML, sessions, service/Agent/peer token exchange, RLS, multi-tenant routing,
SecretReference, Credential Broker, ABAC/Policy decisions, or formal Approval authorization.
Those capabilities remain in the formal L2 module and must build on `PrincipalContext` rather than
weakening this boundary.
