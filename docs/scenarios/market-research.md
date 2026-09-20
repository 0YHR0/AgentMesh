# Scenario: produce a product market brief with a virtual team

English | [简体中文](market-research.zh-CN.md)

## The user problem

Suppose you plan to launch AI-enabled headphones and want a virtual company to answer:

> Who are the main competitors, what do users care about, and which audience, price, and value
> proposition should we test?

The deliverable is a source-aware, independently reviewed market brief rather than an unsupported
chat response.

## Try the workflow without credentials

The repository includes a deterministic Market Intelligence Studio example. It uses fixed fixtures,
makes no internet or model request, and is not a real market conclusion. It demonstrates the
delivery chain without cost:

```bash
export AGENTMESH_FEATURE_PROFILE=full
export AGENTMESH_FEATURE_GATES=company_model=true,company_goals=true,company_operations=true,business_objects=true,organizational_memory=true,company_finance_read=true,financial_governance=true,company_packs=true
docker compose up -d postgres redis migrate api
python examples/market-intelligence-studio/run.py
```

PowerShell users should set the first two values with `$env:NAME="value"`. This offline example
needs the API-side persistence and governance services, but no model Worker.

Use the exact command and output paths documented by the
[Market Intelligence Studio example](../../examples/market-intelligence-studio/README.md).

## Prerequisites for real work

A platform administrator prepares a real model provider and credential, search/read MCP tools,
published Agent Versions, approval rules, and model/tool budgets. Live internet data is not included
by default.

Use four kinds of responsibility. The built-in live-research template further separates research
into `research-lead` and `research-specialist`, followed by `fact-reviewer` and
`editorial-reviewer`:

| Employee | Responsibility | Suggested tools | Permissions to withhold |
|---|---|---|---|
| Research lead/specialist | Plan scope and collect competitors, pricing, reviews, and sources | Search, web read | Publish, pay |
| Data analyst | Compare, classify, and grade the evidence | Read research material | Alter source evidence |
| Product manager | Recommend audience, positioning, price, and experiments | Read analysis | Present estimates as facts |
| Report reviewer | Check citations, contradictions, omissions, and claim strength | Read all intermediate work | Bypass publication approval |

## What an ordinary user enters

Select **Coordinated** in the Console and use an objective such as:

```text
Produce a market brief for AI-enabled headphones priced at CNY 999–1,299 and intended for knowledge
workers in major Chinese cities. Prefer public evidence from the last 90 days. Separate facts,
inferences, and recommendations; cite every material claim. Deliver a competitor table, user pains,
differentiated positioning, major risks, and three validation experiments that can run within two
weeks. Wait for human approval before publication.
```

Configure these work units, replacing every Agent ID with a published, capability-matched ID from
your environment:

| Key | Role | Goal | Dependency key |
|---|---|---|---|
| `research` | Market researcher | Collect source-backed competitor evidence | None |
| `analysis` | Data analyst | Compare evidence and assign confidence | `research` |
| `positioning` | Product manager | Produce positioning and a validation plan | `analysis` |
| `review` | Report reviewer | Approve or request corrections | `positioning` |

A maximum concurrency of `2` is enough for the first run because most work has sequential
dependencies.

## What AgentMesh does

1. Persists the goal as a traceable Task.
2. Selects and pins an Agent Version for every role.
3. Schedules only work whose dependencies are complete; independent work may run in parallel.
4. Transfers structured evidence through Handoffs.
5. Persists state, events, model/tool usage, and intermediate results.
6. Applies review feedback without rewriting completed history.
7. Pauses controlled write actions for human approval.
8. Produces final output and traceable Artifacts.

The Mission Map shows responsibility, dependencies, handoffs, tool calls, and approvals instead of
leaving the user to wait for an opaque chat response.

## Result and limits

The expected package contains a competitor/price comparison, dated sources, user pains and evidence
strength, positioning recommendations, risks, validation experiments, review decisions, and a full
execution trail.

Model output may still be wrong. AgentMesh provides **division of work, governance, execution
evidence, and recovery**; it does not automatically guarantee that a market conclusion is true.

Use this team only when independent research, analysis, and review justify the extra latency and
cost. Use Direct to summarize a single supplied document.

Return to the [five-minute user guide](../getting-started.md), or have an operator continue with the
[administrator and operations best practices](../best-practices.md).
