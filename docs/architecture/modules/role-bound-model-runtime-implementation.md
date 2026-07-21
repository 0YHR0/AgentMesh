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

`AGENTMESH_MODEL_PROVIDER=openai` enables the OpenAI Responses API. The adapter:

- uses the bound version instructions and structured Task/Subtask context;
- sends `store=false` and a tenant-derived, privacy-preserving safety identifier;
- bounds request bytes, response bytes, timeout, and output Tokens;
- extracts only assistant `output_text` items and rejects malformed or empty responses;
- records provider-reported input, output, and total Tokens against the active Attempt;
- never logs or persists the API key; and
- fails the Attempt on provider errors rather than silently substituting deterministic output.

The configured default is `gpt-5.6-terra` with low reasoning effort to balance capability, latency,
and cost. Both values are deployment configuration, not an immutable platform assumption.

## Trust boundaries

- The API key enters only through the Worker environment and is represented as a Pydantic secret.
- Agent instructions come from the digest-bound Registry version, not Task input or the browser.
- Task input remains untrusted context and cannot replace the version instruction boundary.
- The provider adapter has no Tool access in this increment. Existing governed MCP execution remains
  a separate explicit path.
- Provider usage is authoritative for Token accounting; monetary cost is intentionally not guessed.

## Deferred

- per-Agent provider credentials and models;
- additional provider adapters and self-hosted endpoints;
- streaming output, structured output schemas, and multi-turn provider continuation;
- model-driven Tool loops, sandboxing, and context compaction;
- Console Agent catalog management and version publishing.

## Verification

- unit tests cover Registry instruction binding, provider payloads, output parsing, usage reporting,
  deterministic fallback, and digest drift rejection;
- configuration tests enforce explicit provider selection and credentials; and
- the Compose/browser path verifies specialist assignment and full coordinated completion.
