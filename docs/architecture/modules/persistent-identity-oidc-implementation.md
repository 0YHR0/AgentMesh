# Persistent Identity and OIDC baseline implementation

Status: Partial

Feature Gate: `persistent_identity` (explicit opt-in; depends on `identity_rbac`)

## Responsibility

This increment adds a durable identity directory to the existing fail-closed RBAC boundary.
PostgreSQL stores tenant-scoped Principals, exact external identity mappings, and lifecycle-aware
RoleBindings. Configured SHA-256 Bearer credentials remain a bootstrap authentication adapter;
verified OIDC bearer tokens provide the external identity proof.

The identity proof and authorization source are intentionally separate. OIDC claims establish the
configured issuer and subject only. AgentMesh never imports roles from the token and reloads active,
effective, non-expired RoleBindings for every authenticated request.

## Durable model

- `Principal`: stable UUID, tenant, type, status, display name and optimistic revision.
- `ExternalIdentity`: unique tenant + normalized issuer + subject mapping to one Principal.
- `RoleBinding`: role, effective/expiry window, status, creator and immutable revocation evidence.

Principal lifecycle supports `ACTIVE`, `SUSPENDED`, and terminal `DEACTIVATED`. Role revocation is
idempotent and retains actor, reason and timestamp. Administrative writes create tenant-scoped
Outbox audit events. Creation and grants use the shared durable Idempotency repository.

## Authentication flow

1. Parse one bounded HTTP Bearer credential.
2. Constant-time compare it against configured bootstrap digests.
3. Otherwise verify the OIDC JWT using issuer discovery and cached JWKS, an asymmetric algorithm
   allowlist, signature, issuer, audience, `iat`, and `exp`.
4. Resolve exact `(issuer, subject)` to a durable Principal; no just-in-time provisioning occurs.
5. Reject inactive/cross-tenant Principals and Principals with no effective RoleBinding.
6. Return immutable `PrincipalContext`; route authorization continues through stable Permissions.

Discovery and JWKS retrieval are lazy so an unavailable IdP does not prevent the local control
plane from starting. OIDC requests fail closed while the dependency is unavailable.

## Administration API

Tenant administrators can create/list Principals, change Principal status, add an ExternalIdentity,
grant/list RoleBindings, and revoke a RoleBinding. Routes require both `identity:admin` and the
`persistent_identity` Gate. List queries are tenant-scoped and bounded.

Bootstrap configuration uses UUID Principal IDs in persistent mode. A configured Principal and its
initial roles are seeded only when absent. Restart never recreates revoked roles, so PostgreSQL
remains the lifecycle authority after first bootstrap.

## Deliberate boundary

This increment does not implement browser sessions/PKCE, SAML, Groups/SCIM, just-in-time
provisioning, PAT lifecycle, workload token exchange, delegation, RLS, SecretReference,
CredentialBinding, or the Credential Broker. Those remain in the formal module.
