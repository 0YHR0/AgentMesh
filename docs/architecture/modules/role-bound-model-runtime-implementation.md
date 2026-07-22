# Role-bound model runtime implementation

Status: Implemented baseline

## Outcome

The Worker now executes a Run against the exact published `AgentVersion` captured when that Run
was scheduled. It re-reads the version, verifies its content digest, and uses the version role and
instructions as the runtime contract. A mismatch fails closed before provider work begins.

The bootstrap registry includes three general-task specialists:

| Agent | Role | Default Console stage |
|---|---|---|
| `demo-researcher` | Researcher | Gather facts, evidence, assumptions, and gaps |
| `demo-analyst` | Analyst | Compare alternatives, risks, and tradeoffs |
| `demo-synthesizer` | Synthesizer | Reconcile upstream results into a conclusion |

These are separate definitions and immutable published versions. They are not prompt labels applied
to one shared `demo-agent` Run.

## Runtime modes

`AGENTMESH_MODEL_PROVIDER=deterministic` is the default. It needs no external credential and keeps
the local platform demonstration repeatable. Its result records the bound Agent ID, version digest,
and role but does not claim model intelligence or Token usage.

`AGENTMESH_MODEL_PROVIDER=openai` enables the OpenAI Responses API as the deployment default. A
published Agent Version may instead pin its own `model_policy`: `deterministic`, or an OpenAI model,
reasoning effort, output limit, and optional metadata-only `credential_reference_id`. Empty policy
inherits the deployment default, preserving existing Agents and the zero-credential quick start.

The OpenAI adapter:

- uses the bound version instructions and structured Task/Subtask context;
- sends `store=false` and a tenant-derived, privacy-preserving safety identifier;
- bounds request bytes, response bytes, timeout, and output Tokens;
- extracts only assistant `output_text` items and rejects malformed or empty responses;
- records provider-reported input, output, and total Tokens against the active Attempt;
- never logs or persists the API key; and
- fails the Attempt on provider errors rather than silently substituting deterministic output.

## Per-Agent credential boundary

An Agent Version may reference an active tenant-owned `SecretReference` whose purpose is
`MODEL_PROVIDER_API_KEY` and whose audience includes `https://api.openai.com`. The Worker resolves
the external environment key only while constructing that Agent's provider transport. Raw secret
values are never accepted in `model_policy`, returned by APIs, or persisted in the Registry,
Task/Run output, Tool audit, or usage ledger. Missing, revoked, cross-tenant, wrong-purpose, and
wrong-audience references fail closed before a provider request.

## Governed model Tool loop

An Agent Version may pin `tool_profile.allowed_tools` and a `max_calls` budget from 1 to 8. When the
explicit `model_tool_loop` Gate and its governed MCP dependencies are enabled, the Worker:

1. resolves every logical Tool key from the published tenant Catalog;
2. exposes only its pinned description and JSON Schema to the model;
3. rejects every non-read-only capability, even if MCP write execution is separately enabled;
4. re-resolves the binding and sends every call through the existing bounded MCP Gateway and
   durable `ToolInvocation` audit service; and
5. replays complete Responses output items plus `function_call_output` while `store=false`, until
   the model returns final text or exceeds the immutable call budget.

Model-visible function names are deterministic aliases of logical Tool keys. The final Run output
contains call IDs, invocation IDs, Tool/server identity and schema digests, but not raw credentials.
Write-class model Tool calls remain deferred until a separate approval and Permit design exists.

Example Agent Version fields:

```json
{
  "model_policy": {
    "provider": "openai",
    "model": "gpt-5.6-terra",
    "reasoning_effort": "low",
    "max_output_tokens": 1200,
    "credential_reference_id": "00000000-0000-0000-0000-000000000000"
  },
  "tool_profile": {
    "allowed_tools": ["workspace.read_text"],
    "max_calls": 3
  }
}
```

The Tool loop is an advanced opt-in and requires all dependencies, for example:

```dotenv
AGENTMESH_FEATURE_GATES=identity_rbac=true,policy_approval=true,mcp_read_tools=true,governed_mcp=true,model_tool_loop=true
```

Creating the metadata-only SecretReference through the Control API additionally requires the
existing `credential_broker` dependency chain. The referenced environment variable must be present
only in the Worker process.

The configured default is `gpt-5.6-terra` with low reasoning effort to balance capability, latency,
and cost. Both values are deployment configuration, not an immutable platform assumption.

## Trust boundaries

- A deployment-default API key enters only through the Worker environment and is represented as a
  Pydantic secret; per-Agent keys use metadata-only SecretReferences resolved inside the Worker.
- Agent instructions come from the digest-bound Registry version, not Task input or the browser.
- Task input remains untrusted context and cannot replace the version instruction boundary.
- Model Tool access is default-off, bounded by the immutable Agent Version, and limited to governed
  read-only MCP Tools. The older explicit Task `tool_call` path remains supported.
- Provider usage is authoritative for Token accounting; monetary cost is intentionally not guessed.

## Deferred

- additional provider adapters and self-hosted endpoints;
- streaming output, structured output schemas, sandboxing, and context compaction;
- approved write-class model Tool calls and credential lease accounting for model providers;
- Console Agent catalog management and version publishing.

## Verification

- unit tests cover Registry instruction/policy binding, provider payloads, output parsing, usage
  reporting, deterministic fallback, digest drift rejection, credential boundary validation,
  complete `store=false` Tool-loop replay, call budgets, and write-capability rejection;
- configuration tests enforce explicit provider selection and credentials; and
- the Compose/browser path verifies specialist assignment and full coordinated completion.
