from typing import Any

from agentmesh.application.ports import AgentExecutionContext


class DeterministicAgentExecutor:
    """A zero-credential executor used to prove the platform execution path."""

    def execute(
        self,
        *,
        objective: str,
        input: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        return {
            "summary": f"Demo agent completed: {objective}",
            "input": dict(input),
            "agent": {
                "id": context.agent_id,
                "version_id": (str(context.agent_version_id) if context.agent_version_id else None),
                "version_digest": context.agent_version_digest,
                "kind": "deterministic-demo",
            },
            "execution": {
                "task_id": str(context.task_id),
                "run_id": str(context.run_id),
                "thread_id": context.thread_id,
            },
        }
