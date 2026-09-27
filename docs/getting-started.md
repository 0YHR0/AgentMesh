# AgentMesh: a five-minute guide for users

English | [简体中文](getting-started.zh-CN.md)

This guide is for people using AgentMesh for the first time. You do not need to understand
LangGraph, DAGs, Redis, runtimes, or idempotency. The basic loop is: **create work, choose how it
runs, watch the employees collaborate, and inspect the result**.

> AgentMesh is currently Alpha software for evaluation, local development, and non-critical
> single-team use. The default deterministic executor proves the workflow; it is not a benchmark
> of real-model content quality.

## 1. Start AgentMesh

Install Docker, then run:

```bash
git clone https://github.com/0YHR0/AgentMesh.git
cd AgentMesh
docker compose up --build
```

Open <http://localhost:8000>. English is the default; use the language control in the header to
switch to Chinese.

## 2. Run one single-employee task

1. Open **Tasks** and the task creation form.
2. Enter the goal `Summarize these customer comments in three points`, paste the comments into
   **Materials**, and describe the **Expected output**. A file name or URL alone does not give an
   employee access to its contents.
3. Keep **Direct** selected. Choose a published employee, or use the deployment default.
4. Review the plan, then select **Create and run**; or select **Create task** and run it later.
5. Inspect the status, run history, events, and output.

No API key or external model is used by default. This path proves that task creation, queueing,
worker execution, and durable result storage work end to end.

## 3. Use a team only when work benefits from it

Select **Coordinated** only when the goal benefits from specialization, parallel work, or an
independent review. A product market brief might use:

| Role | Goal | Dependency |
|---|---|---|
| Researcher | Collect and organize the supplied evidence | None |
| Analyst | Compare competitors and identify trends | Researcher |
| Product manager | Recommend positioning from the analysis | Analyst |
| Reviewer | Check facts, reasoning, and omissions | Product manager |

For each row, describe its role and deliverable, choose a published employee by name, and select
the preceding steps it needs. Enable coordinated execution first if Setup shows it as unavailable.
Do not split naturally simple work merely to make it look
multi-agent: every handoff adds latency, cost, and potential information loss.

## 4. Configuration responsibilities

An ordinary user supplies the goal, source material, expected deliverable, desired roles, deadline,
and human decision points. A platform administrator configures the model credential, published
Agent Versions, MCP tools, approval rules, and budgets.

The Console supports named OpenAI and DeepSeek connections. An administrator enables
authentication and encrypted credential storage, opens the Console through HTTPS or a verified
local/SSH connection, then configures and explicitly tests the provider. Employees can share one
connection; each employee still has its own published instructions and capabilities.

See [model and memory setup](model-and-memory-setup.md) for the complete walkthrough and release
status. Never put a key in a task or Git, and never enter a provider key or platform access token on
public HTTP. Saving a connection is not proof that the provider accepted it. Other model services
need an appropriate adapter; not every OpenAI-compatible endpoint is qualified.

## 5. What to watch

- **Queued** means the work is accepted and waiting for execution.
- **Running** means an employee is working.
- **Paused / Approval required** means a person must decide or the work is paused.
- **Failed / Unknown** means execution failed or an external outcome cannot yet be proven.
- **Succeeded** means the workflow completed; inspect its output and Artifacts.
- **Mission Map** shows responsibility, dependencies, handoffs, and governed interactions.
- **Run history** keeps each execution of the same Task visible.
- **Artifact** is a report, JSON document, code package, audio file, or other deliverable.

Do not blindly recreate a Task in `Unknown`. An operator should inspect evidence and reconcile the
original execution first so an external side effect is not repeated.

## 6. Current product boundary

| Capability | Current behavior |
|---|---|
| Single- and multi-agent work | Direct, Reviewed, and Coordinated are supported |
| Roles and dependencies | Explicit work units and dependencies are supported |
| Observation and intervention | Status, events, pause, resume, cancel, and approval are supported |
| Recovery | Durable state, bounded retry, and unknown-outcome reconciliation are supported |
| Tools and remote Agents | MCP and A2A are available after configuration |
| Live internet evidence | Not available by default; add search/read MCP tools |
| Email, publishing, or payments | Not available by default; require tools, policy, and approval |
| Long-term employee memory | Optional built-in PostgreSQL memory, reviewed notes, and explicit task scope; no extra Memory API key required |
| Content quality | Depends on the model, prompts, tools, evidence, and review design |

Continue with the [real-model customer-feedback walkthrough](scenarios/customer-feedback-deepseek.md),
or explore the [market-research scenario](scenarios/market-research.md). Operators and platform
developers should use the [administrator and operations best practices](best-practices.md).

## 7. Plain-language glossary

| Product term | Plain-language meaning |
|---|---|
| Agent | A virtual employee |
| Agent Version | One fixed, traceable training/configuration version of that employee |
| Capability | What an employee can do |
| Tool / MCP | An external tool / the standard way to connect it |
| Task | A piece of work requested by a user |
| Run | One formal execution of that work |
| Direct | One employee completes the work |
| Reviewed | One employee completes it and another independently reviews it |
| Coordinated | Several employees work through explicit dependencies |
| Handoff | One employee transfers structured work to another |
| Artifact | A report, code package, audio file, or other deliverable |
| Approval | Human confirmation before a controlled action |
| Budget | A limit on model, tool, or monetary usage |
