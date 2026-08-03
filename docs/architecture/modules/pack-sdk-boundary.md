# Company Pack SDK and Scenario Boundary

Status: Implemented phase 1

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

Music Studio now lives under `agentmesh.packs.music_studio`. The old
`agentmesh.templates.music_studio` module is a compatibility-only re-export so downstream imports
do not break during the transition.

## Dependency direction

```text
Music Studio definition -> Pack SDK -> Company Pack domain
Company Template API ----> Catalog ----> definition
Company Pack service ----> Pack SDK contract
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

Phase 1 separates the declarative Pack and configuration contract. Later phases can move the
Music Studio workflow service, HTTP routes, provider adapters, and static UI into a separately
versioned distribution. The older Market Intelligence template and Operations helpers must also
move onto the same definition contract before the whole application layer is scenario-neutral.
Physical repository separation should wait until the SDK compatibility policy and external Pack
loading/trust model are stable.
