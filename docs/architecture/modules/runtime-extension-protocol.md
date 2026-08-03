# Runtime Extension Protocol

Status: Implemented v0.1 (trusted in-process)

AgentMesh Runtime Extension API v0.1 is the executable boundary between the operating core and a
business scenario. The core owns durable Tasks, Agents, governance, persistence, credentials,
memory, Artifacts, and observability. An extension owns its workflow service, provider adapters,
HTTP API, focused workspace, health probe, and Company Template definitions.

The contract lives in `agentmesh.extensions.sdk`. The current API version is `0.1`.

## Contract

A `RuntimeExtensionDefinition` provides:

- an immutable `ExtensionManifest`;
- a service factory receiving an `ExtensionContext`;
- an API registrar for routes and workspace assets;
- zero or more `CompanyTemplateDefinition` values;
- a health probe and idempotent stop callback.

The manifest declares the extension identifier and semantic version, Runtime API version, required
core services, provided namespaced services, Features, Credentials, permissions, workspaces, and
whether external writes are enabled. AgentMesh validates identifiers, versions, Feature names,
service surfaces, duplicate extension IDs, and extension/core route or workspace/asset collisions
before serving work.

The service factory cannot receive the `ApplicationContainer` or database engine. It receives a
read-only mapping of explicitly exposed core capabilities through stable `CoreServiceKey` names.
This is an API boundary, not a process sandbox.

## Discovery and enablement

Built-in and installed extensions share `RuntimeExtensionRegistry`. A separately distributed
Python package publishes this entry point:

```toml
[project.entry-points."agentmesh.runtime_extensions"]
my_scenario = "my_scenario.extension:EXTENSION"
```

Production discovery is additionally constrained by the repository's `extensions.lock`. Before
calling an installed Entry Point, AgentMesh matches its distribution name, installed version, and
Entry Point name to a locked record. An unlisted installed Entry Point fails startup before its
Python code is imported. After import, its manifest identifier, version, Features, Credentials,
permissions, and external-write declaration must exactly match the lock.

The operator verifies and installs a wheel with:

```bash
agentmesh-extension-install ./extension.whl \
  --extension-id community.daily-brief \
  --lock extensions.lock
```

The installer reads wheel `METADATA` and `entry_points.txt` as data, streams its SHA-256, and
compares them with the lock before invoking `pip install --no-deps`. After a successful install it
appends `.agentmesh/extensions/install-audit.jsonl` with the actor, source, version, and verified
digest. `--verify-only` performs preflight without installing. Dependency installation remains an
explicit image/operator responsibility in v0.1.

Enabled extensions are configured as a comma-separated allowlist:

```dotenv
AGENTMESH_RUNTIME_EXTENSIONS=agentmesh.music-studio
```

An empty value disables all installed extensions. `*` enables all discovered extensions and cannot
be combined with explicit identifiers. Unknown identifiers fail startup instead of being ignored.

## Lifecycle

```text
discover -> validate -> load services -> probe -> serve -> stop
```

API routes are registered from validated definitions while constructing the FastAPI application.
An enabled extension then creates exactly its declared service keys. Disabled extensions remain
visible to operators but service and workspace access fails closed with HTTP 503. Missing optional
Feature activation produces a `degraded` status; route-level Feature gates still enforce the
operation. `ApplicationContainer.close()` invokes every loaded extension stop callback once.

Operators can inspect the effective state at `GET /api/v1/extensions`. The response discloses
version, health, missing Features, required Credentials and permissions, service keys, workspace
routes, external-write boundary, trust level, source, locked distribution/Entry Point/digest, and
whether the running manifest passed the lock.

## Trust levels and boundary

- `built-in`: shipped and locked with AgentMesh source;
- `verified`: externally distributed release approved by the lock owner;
- `local`: locally reviewed or built artifact pinned by SHA-256;
- `unverified`: deliberately pinned but not independently reviewed;
- `unmanaged`: SDK/test registries created without a lock; never used by the production
  composition root.

The SHA-256 proves that the selected wheel equals the operator-pinned bytes; it does not establish
who authored those bytes. The JSONL receipt is append-only by convention, not cryptographically
immutable. Installed extensions remain trusted same-process Python code. The lock therefore
reduces accidental or undeclared loading and makes approval reviewable, but does not contain a
malicious extension.

## Music Studio proof

Music Studio is the first implementation. The core API and bootstrap modules do not import its
runtime, routes, console, or provider code. The built-in registry discovers one definition, the
generic runtime supplies its required services, and its registrar preserves these URLs:

- `/music-studio`;
- `/console/assets/music-studio.*`;
- `/api/v1/music-studio/*`.

Its Company Template is also projected into `PackCatalog`, so declarative installation and
executable runtime discovery come from one scenario definition.

## External repository proof

[AgentMesh Extension Starter](https://github.com/0YHR0/AgentMesh-Extension-Starter) is maintained
as an independent repository and Python distribution. Its `community.daily-brief` extension
provides a deterministic service, API, workspace, static assets, and health probe without changing
AgentMesh source code. Its CI checks out AgentMesh, installs both distributions, runs extension
tests and lint, and builds the wheel. Installing that wheel makes the existing entry-point registry
discover both Music Studio and Daily Brief in the same process.

## Deliberate v0.1 limits

- extensions run in the AgentMesh API process and have the privileges of that process;
- installing, removing, or changing the allowlist requires a restart;
- extension-owned database migrations are not accepted;
- package signatures/attestations, registry policy, automatic dependency resolution, hot reload,
  process isolation, and remote A2A extensions remain later protocol versions.

The next security step should follow real deployment demand: signature/attestation verification
for public distribution, then process isolation for extensions that should not share API
privileges.
