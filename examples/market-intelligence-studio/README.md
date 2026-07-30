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
