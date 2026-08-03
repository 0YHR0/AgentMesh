# Company Pack SDK and Scenario Boundary

Status: Implemented phase 2

AgentMesh keeps the operating runtime independent from business scenarios. The runtime owns Tasks,
Agents, governance, persistence, Pack installation, compatibility checks, upgrades, and audit.
A scenario owns its organization manifest, business vocabulary, configuration validation, required
capabilities, workflow adapters, and focused user experience.

The first stable in-process SDK surface is `agentmesh.packs.sdk`:

- `CompanyTemplateDefinition` declares identity, semantic version, mission, manifest factory,
  Feature requirements, credentials, permissions, safety default, and configuration normalizer;
- `PackCatalog` provides deterministic discovery and rejects ambiguous slug or Pack-key
  registration;
- `CompanyPackService` consumes the definition contract and contains no Music Studio imports or
  music-specific validation;
- the API resolves Music Studio through the built-in Catalog and passes its definition into generic
  preview, installation, upgrade-preview, and upgrade operations.

Music Studio now lives under `agentmesh.packs.music_studio`. It owns its definition, runtime
service, deterministic provider adapters, HTTP routes, and focused workspace assets. The API and
bootstrap modules are composition roots: they explicitly attach the scenario to AgentMesh without
moving scenario behavior back into the generic application layer.

The old `agentmesh.templates.music_studio`,
`agentmesh.application.music_studio_services`, `agentmesh.integrations.music.deterministic`, and
`agentmesh.api.music_studio_routes` modules are compatibility-only re-exports so downstream
imports do not break during the transition. The public `/api/v1/music-studio`, `/music-studio`,
and `/console/assets/music-studio.*` URLs are unchanged.

## Dependency direction

```text
Music Studio definition -> Pack SDK -> Company Pack domain
Company Template API ----> Catalog ----> definition
Company Pack service ----> Pack SDK contract
API/bootstrap roots ------> Music Studio runtime/routes/console
```

The Company Pack service must not import a concrete scenario. Scenario configuration validation
must not be added to the generic service. External writes remain disabled by default at the SDK
boundary.

## Adding a scenario

An in-repository scenario implements a `CompanyTemplateDefinition`, registers it in a Catalog, and
uses the generic service operations. Its manifest remains declarative; executable code is not
loaded from Pack JSON. A future external repository will publish a Python package or signed bundle
that exposes the same definition contract after a separate trust and loading design is accepted.

## Remaining separation work

Phases 1 and 2 separate both the declarative contract and the complete Music Studio implementation
inside the Python package. Runtime Extension API v0.1 now discovers its executable surface through
a generic trusted in-process contract, so the core application and bootstrap no longer import
Music Studio code. A later phase can publish the scenario as a separately versioned distribution
after the signed installation/trust model is stable. The older Market Intelligence template and
Operations helpers must also move onto the same definition contract before the whole application
layer is scenario-neutral.
