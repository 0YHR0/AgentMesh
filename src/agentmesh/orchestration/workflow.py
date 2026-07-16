from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from typing_extensions import NotRequired, TypedDict

from agentmesh.application.ports import AgentExecutionContext, AgentExecutor
from agentmesh.domain.tasks import Task, TaskRun


class AgentGraphState(TypedDict):
    task_id: str
    run_id: str
    thread_id: str
    objective: str
    input: dict[str, Any]
    agent_id: str
    agent_version_id: str | None
    agent_version_digest: str | None
    output: NotRequired[dict[str, Any]]


class LangGraphWorkflowRunner:
    def __init__(
        self,
        *,
        agent_executor: AgentExecutor,
        checkpointer: BaseCheckpointSaver[Any],
        callbacks: list[Any] | None = None,
    ) -> None:
        self._agent_executor = agent_executor
        self._callbacks = list(callbacks or [])

        graph_builder = StateGraph(AgentGraphState)
        graph_builder.add_node("execute_agent", self._execute_agent)
        graph_builder.add_edge(START, "execute_agent")
        graph_builder.add_edge("execute_agent", END)
        self._graph = graph_builder.compile(checkpointer=checkpointer)

    def run(self, task: Task, run: TaskRun) -> dict[str, Any]:
        config: dict[str, Any] = {
            "configurable": {"thread_id": run.thread_id},
            "run_name": "agentmesh-task-run",
            "metadata": {
                "task_id": str(task.id),
                "run_id": str(run.id),
                "agent_id": run.agent_id,
                "agent_version_id": (str(run.agent_version_id) if run.agent_version_id else None),
                "agent_version_digest": run.agent_version_digest,
            },
        }
        if self._callbacks:
            config["callbacks"] = self._callbacks

        checkpoint = self._graph.get_state(config)
        checkpoint_output = checkpoint.values.get("output") if checkpoint.values else None
        if not checkpoint.next and isinstance(checkpoint_output, dict):
            return dict(checkpoint_output)

        state: AgentGraphState = {
            "task_id": str(task.id),
            "run_id": str(run.id),
            "thread_id": run.thread_id,
            "objective": task.objective,
            "input": dict(task.input),
            "agent_id": run.agent_id,
            "agent_version_id": str(run.agent_version_id) if run.agent_version_id else None,
            "agent_version_digest": run.agent_version_digest,
        }
        result = self._graph.invoke(state, config=config)
        output = result.get("output")
        if not isinstance(output, dict):
            raise TypeError("Agent workflow output must be a JSON object")
        return output

    def _execute_agent(self, state: AgentGraphState) -> dict[str, Any]:
        context = AgentExecutionContext(
            task_id=self._parse_uuid(state["task_id"]),
            run_id=self._parse_uuid(state["run_id"]),
            thread_id=state["thread_id"],
            agent_id=state["agent_id"],
            agent_version_id=(
                self._parse_uuid(state["agent_version_id"]) if state["agent_version_id"] else None
            ),
            agent_version_digest=state["agent_version_digest"],
        )
        output = self._agent_executor.execute(
            objective=state["objective"],
            input=state["input"],
            context=context,
        )
        return {"output": output}

    @staticmethod
    def _parse_uuid(value: str):
        from uuid import UUID

        return UUID(value)


def create_langfuse_callbacks(enabled: bool) -> list[Any]:
    if not enabled:
        return []

    from langfuse.langchain import CallbackHandler

    return [CallbackHandler()]
