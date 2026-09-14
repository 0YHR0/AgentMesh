from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, uuid5

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from typing_extensions import NotRequired, TypedDict

from agentmesh.application.coordinated_runtime_delivery import CoordinatedDeliveryLeaseV1
from agentmesh.application.ports import (
    AgentExecutionContext,
    AgentExecutor,
    AttemptTelemetry,
    WorkflowExecutionResult,
    WorkflowWorkItem,
)
from agentmesh.application.runtime_work_items import CanonicalWorkItemBuilder
from agentmesh.domain.observability import UsageRecord
from agentmesh.domain.tasks import RunRole, Task, TaskAttempt, TaskRun
from agentmesh.observability import NoOpAttemptTelemetry


class AgentGraphState(TypedDict):
    tenant_id: str
    task_id: str
    run_id: str
    attempt_id: str
    trace_id: str
    thread_id: str
    objective: str
    input: dict[str, Any]
    agent_id: str
    agent_version_id: str | None
    agent_version_digest: str | None
    run_role: str
    revision_number: int
    output: NotRequired[dict[str, Any]]
    usage_records: NotRequired[list[dict[str, Any]]]


class LangGraphWorkflowRunner:
    def __init__(
        self,
        *,
        agent_executor: AgentExecutor,
        reviewer_executor: AgentExecutor | None = None,
        checkpointer: BaseCheckpointSaver[Any],
        telemetry: AttemptTelemetry | None = None,
        work_item_builder: CanonicalWorkItemBuilder | None = None,
    ) -> None:
        self._agent_executor = agent_executor
        self._reviewer_executor = reviewer_executor or agent_executor
        self._telemetry = telemetry or NoOpAttemptTelemetry()
        self._work_item_builder = work_item_builder or CanonicalWorkItemBuilder()

        graph_builder = StateGraph(AgentGraphState)
        graph_builder.add_node("execute_agent", self._execute_agent)
        graph_builder.add_edge(START, "execute_agent")
        graph_builder.add_edge("execute_agent", END)
        self._graph = graph_builder.compile(checkpointer=checkpointer)

    def run(
        self,
        task: Task,
        run: TaskRun,
        attempt: TaskAttempt,
        work_item: WorkflowWorkItem | None = None,
    ) -> WorkflowExecutionResult:
        if work_item is None:
            work_item = self._work_item_builder.build(task, run)
        return self._run_graph(
            state={
                "tenant_id": task.tenant_id,
                "task_id": str(task.id),
                "run_id": str(run.id),
                "attempt_id": str(attempt.id),
                "trace_id": attempt.trace_id,
                "thread_id": run.thread_id,
                "objective": work_item.objective,
                "input": dict(work_item.input),
                "agent_id": run.agent_id,
                "agent_version_id": str(run.agent_version_id) if run.agent_version_id else None,
                "agent_version_digest": run.agent_version_digest,
                "run_role": run.role.value,
                "revision_number": run.revision_number,
            },
            config={
                "configurable": {"thread_id": run.thread_id},
                "run_name": "agentmesh-task-run",
                "metadata": {
                    "task_id": str(task.id),
                    "run_id": str(run.id),
                    "attempt_id": str(attempt.id),
                    "trace_id": attempt.trace_id,
                    "agent_id": run.agent_id,
                    "agent_version_id": (
                        str(run.agent_version_id) if run.agent_version_id else None
                    ),
                    "agent_version_digest": run.agent_version_digest,
                    "run_role": run.role.value,
                    "revision_number": run.revision_number,
                },
            },
            observe=self._telemetry.observe_attempt(task, run, attempt),
        )

    def run_delivery(
        self,
        lease: CoordinatedDeliveryLeaseV1,
        work_item: WorkflowWorkItem,
    ) -> WorkflowExecutionResult:
        """Execute a detached delivery without materializing domain entities."""
        if work_item != lease.work_item:
            raise ValueError("Detached delivery work item conflicts with its lease")
        agent_id = str(uuid5(NAMESPACE_URL, f"agentmesh:agent:{lease.agent_version_id}"))
        thread_id = str(lease.run_id)
        trace_id = lease.attempt_id.hex
        return self._run_graph(
            state={
                "tenant_id": lease.tenant_id,
                "task_id": str(lease.task_id),
                "run_id": str(lease.run_id),
                "attempt_id": str(lease.attempt_id),
                "trace_id": trace_id,
                "thread_id": thread_id,
                "objective": work_item.objective,
                "input": dict(work_item.input),
                "agent_id": agent_id,
                "agent_version_id": str(lease.agent_version_id),
                "agent_version_digest": lease.agent_version_digest,
                "run_role": lease.role.value,
                "revision_number": lease.run_revision,
            },
            config={
                "configurable": {"thread_id": thread_id},
                "run_name": "agentmesh-delivery",
                "metadata": {
                    "task_id": str(lease.task_id),
                    "run_id": str(lease.run_id),
                    "attempt_id": str(lease.attempt_id),
                    "trace_id": trace_id,
                    "agent_id": agent_id,
                    "agent_version_id": str(lease.agent_version_id),
                    "agent_version_digest": lease.agent_version_digest,
                    "run_role": lease.role.value,
                    "revision_number": lease.run_revision,
                },
            },
            observe=self._telemetry.observe_delivery(lease),
        )

    def _run_graph(
        self,
        *,
        state: AgentGraphState,
        config: dict[str, Any],
        observe: Any,
    ) -> WorkflowExecutionResult:
        """Run or resume the shared graph using a prebuilt detached state."""
        with observe:
            checkpoint = self._graph.get_state(config)
            checkpoint_output = checkpoint.values.get("output") if checkpoint.values else None
            if not checkpoint.next and isinstance(checkpoint_output, dict):
                return WorkflowExecutionResult(
                    output=dict(checkpoint_output),
                    usage_records=self._usage_from_state(checkpoint.values),
                )

            result = self._graph.invoke(state, config=config)
            output = result.get("output")
            if not isinstance(output, dict):
                raise TypeError("Agent workflow output must be a JSON object")
            return WorkflowExecutionResult(
                output=dict(output),
                usage_records=self._usage_from_state(result),
            )

    def _execute_agent(self, state: AgentGraphState) -> dict[str, Any]:
        usage_records: list[UsageRecord] = []

        def report_usage(record: UsageRecord) -> None:
            usage_records.append(record)
            self._telemetry.record_usage(record)

        context = AgentExecutionContext(
            task_id=self._parse_uuid(state["task_id"]),
            run_id=self._parse_uuid(state["run_id"]),
            thread_id=state["thread_id"],
            agent_id=state["agent_id"],
            agent_version_id=(
                self._parse_uuid(state["agent_version_id"]) if state["agent_version_id"] else None
            ),
            agent_version_digest=state["agent_version_digest"],
            run_role=state["run_role"],
            revision_number=state["revision_number"],
            tenant_id=state["tenant_id"],
            attempt_id=self._parse_uuid(state["attempt_id"]),
            trace_id=state["trace_id"],
            usage_reporter=report_usage,
        )
        executor = (
            self._reviewer_executor
            if state["run_role"] == RunRole.REVIEWER.value
            else self._agent_executor
        )
        output = executor.execute(
            objective=state["objective"],
            input=state["input"],
            context=context,
        )
        return {
            "output": output,
            "usage_records": [record.to_checkpoint() for record in usage_records],
        }

    @staticmethod
    def _usage_from_state(state: dict[str, Any]) -> tuple[UsageRecord, ...]:
        values = state.get("usage_records", [])
        if not isinstance(values, list):
            raise TypeError("Agent workflow usage records must be a list")
        return tuple(UsageRecord.from_checkpoint(value) for value in values)

    @staticmethod
    def _parse_uuid(value: str):
        from uuid import UUID

        return UUID(value)
