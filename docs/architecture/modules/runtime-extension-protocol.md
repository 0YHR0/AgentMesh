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

Only packages explicitly installed by the operator are discoverable. Installed entry points are
trusted Python code and are imported during discovery, so operators must not install untrusted
extension packages. Runtime enablement controls service creation and use; it is not a defense
against malicious package import-time code.

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
routes, and the external-write boundary.

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
- signed bundles, registry trust policy, dependency resolution, hot reload, process isolation, and
  remote A2A extensions remain later protocol versions.

The next security step should be a signed installation/preflight model. Process isolation can now
follow the independently maintained Daily Brief proof instead of being designed against only a
built-in scenario.
