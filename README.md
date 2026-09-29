# AgentMesh

[English](README.md) | [简体中文](README.zh-CN.md)

[![CI](https://github.com/0YHR0/AgentMesh/actions/workflows/ci.yml/badge.svg)](https://github.com/0YHR0/AgentMesh/actions/workflows/ci.yml)
[![CodeQL](https://github.com/0YHR0/AgentMesh/actions/workflows/codeql.yml/badge.svg)](https://github.com/0YHR0/AgentMesh/actions/workflows/codeql.yml)

AgentMesh is a self-hostable control plane for AI-agent teams. Give it a goal and acceptance
criteria; assign specialist employees; then follow their work, handoffs, decisions, and results
in one Console. Simple work can stay with one agent.

**Status:** Alpha. The supported scope is a single-team evaluation or non-critical deployment,
not a production-grade multi-tenant or high-availability service. See the
[implementation status](docs/implementation-status.md) and [v1 boundaries](docs/v1-completion-scope.md).

## Try it locally

Docker is the only prerequisite. The default deterministic executor needs no model API key.

```bash
git clone https://github.com/0YHR0/AgentMesh.git
cd AgentMesh
docker compose up --build
```

Open the [Console](http://localhost:8000), create a Direct task, and inspect its result. To use a
real model, employees, and a coordinated task, follow the guided tutorials below. **Do not enter
provider keys on an unauthenticated public HTTP deployment.**

## Find the right guide

| I want to… | Read |
| --- | --- |
| Create my first task without a key | [Five-minute guide](docs/getting-started.md) · [中文](docs/getting-started.zh-CN.md) |
| Configure DeepSeek and watch three employees collaborate | [Real-model walkthrough with screenshots](docs/scenarios/customer-feedback-deepseek.md) · [中文](docs/scenarios/customer-feedback-deepseek.zh-CN.md) |
| Set up model connections and employee memory | [Model and memory setup](docs/model-and-memory-setup.md) · [中文](docs/model-and-memory-setup.zh-CN.md) |
| Deploy, configure gates, or operate the service | [Administrator best practices](docs/best-practices.md) · [中文](docs/best-practices.zh-CN.md) |
| Understand architecture or extend the platform | [Documentation map](docs/README.md) · [中文](docs/README.zh-CN.md) |

The [documentation map](docs/README.md) also links scenarios, API and feature references,
operations runbooks, architecture decisions, proposals, and the roadmap.

## What is in the core?

- Direct, reviewed, and coordinated task execution with versioned agents and durable results.
- A Console for task creation, status, intervention, and a replayable Mission Map.
- Optional MCP tools, A2A delegation, human approval, budgets, company records, and memory.
- PostgreSQL as the business source of truth; Redis Streams for delivery. LangGraph is an execution
  adapter, while feature gates keep advanced capabilities opt-in.

See [Contributing](CONTRIBUTING.md), [Changelog](CHANGELOG.md), and the
[Apache 2.0 license](LICENSE).
