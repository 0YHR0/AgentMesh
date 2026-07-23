# Governed research brief showcase

This opt-in fixture creates one durable Task that demonstrates the AgentMesh Mission Map without
calling paid model APIs or an external Agent. It writes ordinary AgentMesh domain records to the
local PostgreSQL database; the Console reads them through the same authorized Task and interaction
APIs used in normal operation.

The scenario includes:

- a four-station research, analysis, review, and publication DAG;
- a verified and applied Plan Patch that inserts the review station;
- one failed and one successful execution Attempt;
- an accepted structured Handoff from the researcher to the reviewer;
- a completed read-only MCP Tool invocation;
- an approved external-delegation gate; and
- an A2A remote review correlation waiting for the remote outcome.

The remote peer and Tool result are bounded deterministic fixture records. The script does not send
network traffic, portray hidden model reasoning, or claim that the scenario is a benchmark.

Run it after starting AgentMesh:

```bash
AGENTMESH_FEATURE_PROFILE=full docker compose up -d
docker compose --profile showcase run --rm showcase
```

On PowerShell, set `$env:AGENTMESH_FEATURE_PROFILE="full"` before the first command. The full
profile is required because the default minimal profile intentionally hides advanced activity and
governance views.

Then open <http://localhost:8000>, select the Task whose title starts with `[Showcase]`, and use the
Mission Map filters to isolate Handoff, MCP, A2A, Policy, or Plan Patch interactions. Running the
command again creates a new independent showcase Task and never overwrites existing Tasks.
