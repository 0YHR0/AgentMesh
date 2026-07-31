# Market Intelligence Studio example

This example creates the built-in virtual company, enables its optional Operations Pack, and
records one small, fully offline research chain:

```text
Research Question -> Source Record -> Claim Register -> Research Report
```

The Operations Pack creates a cycle, objective/KRs, Initiative, budget, Memory Policy, and three
recurring Operations. The script asserts that all three Operations remain `DRAFT`. It does not
need a model API key and does not perform external writes. The fixture uses a local source excerpt
and a checked-in expected report so the governance path can be tested before any real MCP tools,
model providers, or commercial connectors are enabled.

## Run

Start AgentMesh with the Company features enabled on a clean tenant:

```bash
export AGENTMESH_FEATURE_PROFILE=full
export AGENTMESH_FEATURE_GATES=company_model=true,company_goals=true,company_operations=true,business_objects=true,organizational_memory=true,company_finance_read=true,financial_governance=true,company_packs=true
docker compose up -d postgres redis migrate api
python examples/market-intelligence-studio/run.py
```

On PowerShell, use:

```powershell
$env:AGENTMESH_FEATURE_PROFILE="full"
$env:AGENTMESH_FEATURE_GATES="company_model=true,company_goals=true,company_operations=true,business_objects=true,organizational_memory=true,company_finance_read=true,financial_governance=true,company_packs=true"
docker compose up -d postgres redis migrate api
python examples/market-intelligence-studio/run.py
```

Set `AGENTMESH_BASE_URL` when the API is not at `http://localhost:8000`. If Identity/RBAC is
enabled, also set `AGENTMESH_TOKEN`.

The script fails unless the Operations Pack stays inside its safety boundary, all recurring
Operations remain `DRAFT`, every object reaches `APPROVED`, every approval carries an evidence
reference, and the final report points back to the approved Claim Register.

## Launch a live governed study

The Admin Console now exposes a provider-neutral live-research preflight and launcher. Keep the
offline example as the zero-credential smoke test; live research additionally requires:

1. enable `identity_rbac`, `policy_approval`, `governed_mcp`, and the Company gates above;
2. register and publish one or more MCP Server Versions so the Catalog resolves exactly one
   read-only binding for each logical key: `web.search` and `source.read`;
3. create published async Agent Versions for `research-lead`, `research-specialist`,
   `fact-reviewer`, and `editorial-reviewer`, with all Position capabilities verified;
4. allow both logical tools in the Research Lead and Research Specialist Version `tool_profile`,
   then appoint all four Versions to their Positions;
5. configure a Credential Broker binding when either MCP server reports
   `authentication_required=true`.

Inspect readiness without creating anything:

```bash
curl http://localhost:8000/api/v1/company-templates/market-intelligence-studio/research/preflight
```

Launch the five-stage coordinated Task:

```bash
curl -X POST \
  http://localhost:8000/api/v1/company-templates/market-intelligence-studio/research/launch \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: first-live-study" \
  -d '{
    "question":"Which governance controls matter most to enterprise AI buyers?",
    "target_audience":"Product and strategy leaders",
    "decision_supported":"Prioritize the next product release",
    "scope":"Public evidence from the last twelve months",
    "max_sources":12
  }'
```

The launcher persists a draft `research-question` Business Object and starts this governed chain:

```text
scope-plan -> evidence-collection -> claim-synthesis -> fact-check -> report-draft
```

Source collection can call only the appointed Version's allowlisted read-only tools. The Task
contract requires attributable source metadata, claim-to-source links, confidence and limitations.
The final deliverable remains an internal draft; external publication and customer delivery are
not part of this endpoint.
