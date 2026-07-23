from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from agentmesh.application.planning_services import PlanningApplicationService
from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.application.services import TaskApplicationService
from agentmesh.domain.a2a_delegation import RemoteTaskCorrelation
from agentmesh.domain.a2a_registry import A2APeer, A2ATrustTier, AgentCardSnapshot
from agentmesh.domain.coordination import CoordinatedPlan, SubtaskSpec
from agentmesh.domain.handoffs import Handoff
from agentmesh.domain.policy import (
    ApprovalOutcome,
    GovernedAction,
    GovernedActionType,
    PolicyResult,
)
from agentmesh.domain.tasks import TaskAttempt, TaskExecutionMode, TaskRun, utc_now
from agentmesh.domain.tools import (
    ToolBinding,
    ToolCallResult,
    ToolInvocation,
    ToolSideEffect,
)


@dataclass(frozen=True)
class ShowcaseResult:
    task_id: str
    objective: str
    interaction_count: int


class ResearchBriefShowcaseService:
    """Create a durable, explicitly labeled Mission Map demonstration fixture."""

    def __init__(
        self,
        *,
        task_service: TaskApplicationService,
        planning_service: PlanningApplicationService,
        uow_factory: UnitOfWorkFactory,
        tenant_id: str,
    ) -> None:
        self._task_service = task_service
        self._planning_service = planning_service
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id

    def create(self) -> ShowcaseResult:
        objective = "[Showcase] Produce a governed, evidence-backed research brief"
        initial_specs = (
            self._spec("research", "Collect source evidence", "demo-researcher"),
            self._spec(
                "analysis",
                "Analyze claims and risks",
                "demo-analyst",
                depends_on=("research",),
            ),
            self._spec(
                "publish",
                "Publish the approved brief",
                "demo-synthesizer",
                depends_on=("analysis",),
            ),
        )
        aggregate = self._task_service.create_task(
            objective,
            {"showcase": "research-brief", "fixture": True},
            execution_mode=TaskExecutionMode.COORDINATED,
            coordinated_plan=CoordinatedPlan.create(initial_specs, max_concurrency=2),
            goal_constraints=("Keep evidence traceable", "Require governed external access"),
            goal_success_criteria=("Produce one reviewed brief",),
        )
        task = aggregate.task
        patched_specs = (
            initial_specs[0],
            initial_specs[1],
            self._spec(
                "review",
                "Review evidence and external findings",
                "demo-reviewer",
                depends_on=("analysis",),
            ),
            self._spec(
                "publish",
                "Publish the approved brief",
                "demo-synthesizer",
                depends_on=("review",),
            ),
        )
        patch = self._planning_service.propose_patch(
            task.id,
            base_plan_version=task.plan_version or 1,
            base_plan_digest=task.plan_digest or "",
            specs=patched_specs,
            max_concurrency=2,
            reason="Insert an independent review gate before publication",
            requested_by="showcase-operator",
        )
        self._planning_service.apply_patch(task.id, patch.id)
        aggregate = self._task_service.get_task(task.id)
        subtasks = {subtask.key: subtask for subtask in aggregate.subtasks}

        research_run = TaskRun.request(
            task.id, "demo-researcher", subtask_id=subtasks["research"].id
        )
        analysis_run = TaskRun.request(
            task.id, "demo-analyst", subtask_id=subtasks["analysis"].id
        )
        review_run = TaskRun.request(
            task.id, "demo-reviewer", subtask_id=subtasks["review"].id
        )
        now = utc_now()
        failed_attempt = TaskAttempt.lease(
            run_id=research_run.id,
            worker_id="showcase-worker",
            fencing_token=1,
            lease_expires_at=now + timedelta(minutes=5),
        )
        failed_attempt.fail("Transient source timeout; retry scheduled")
        successful_attempt = TaskAttempt.lease(
            run_id=research_run.id,
            worker_id="showcase-worker",
            fencing_token=2,
            lease_expires_at=now + timedelta(minutes=5),
        )
        successful_attempt.succeed()

        research_run.start()
        research_run.succeed({"evidence_count": 4, "retry_count": 1})
        analysis_run.start()
        analysis_run.succeed({"risk_count": 2, "recommendation_count": 1})
        review_run.start()

        research = subtasks["research"]
        research.queue(research_run.id)
        research.start(research_run.id)
        research.complete(research_run.id, {"evidence_count": 4})
        analysis = subtasks["analysis"]
        analysis.mark_ready()
        analysis.queue(analysis_run.id)
        analysis.start(analysis_run.id)
        analysis.complete(analysis_run.id, {"risk_count": 2})
        review = subtasks["review"]
        review.mark_ready()
        review.queue(review_run.id)
        review.start(review_run.id)

        handoff = Handoff.request(
            task_id=task.id,
            source_subtask_id=research.id,
            source_run_id=research_run.id,
            source_trace_id=successful_attempt.trace_id,
            source_agent_id=research_run.agent_id,
            target_subtask_id=review.id,
            target_agent_id=review_run.agent_id,
            objective="Review the collected evidence contract",
            reason="Independent verification is required before publication",
            completed_work_summary="Four evidence records were normalized and hashed",
            requested_by=research_run.agent_id,
        )
        handoff.accept(actor=review_run.agent_id, reason="Evidence contract accepted")

        invocation = ToolInvocation.start(
            tenant_id=self._tenant_id,
            task_id=task.id,
            run_id=research_run.id,
            binding=ToolBinding(
                logical_key="workspace.read_text",
                server_name="agentmesh-workspace",
                tool_name="read_text",
                side_effect=ToolSideEffect.READ_ONLY,
            ),
            arguments={"path": "examples/research-brief/evidence.md"},
        )
        invocation.succeed(
            ToolCallResult(
                output={"evidence_records": 4},
                protocol_version="2025-06-18",
                schema_digest=f"sha256:{'1' * 64}",
                result_digest=f"sha256:{'2' * 64}",
                result_bytes=384,
            )
        )

        registered_peer = A2APeer.register(
            tenant_id=self._tenant_id,
            owner_id="showcase-operator",
            name=f"brief-review-{uuid4().hex[:8]}",
            discovery_url="https://review.example/.well-known/agent-card.json",
            allowed_endpoint_hosts=("review.example",),
            allowed_bindings=("HTTP+JSON",),
            trust_tier=A2ATrustTier.TRUSTED,
        )
        snapshot = AgentCardSnapshot.import_card(
            tenant_id=self._tenant_id,
            peer=registered_peer,
            ttl_seconds=3600,
            raw_card={
                "name": "Remote evidence reviewer",
                "description": "Checks public evidence claims",
                "version": "1.0",
                "supportedInterfaces": [
                    {
                        "url": "https://review.example/a2a/v1",
                        "protocolBinding": "HTTP+JSON",
                        "protocolVersion": "1.0",
                    }
                ],
                "capabilities": {},
                "defaultInputModes": ["application/json"],
                "defaultOutputModes": ["application/json"],
                "skills": [
                    {
                        "id": "evidence-review",
                        "name": "Evidence review",
                        "description": "Review structured claims",
                        "tags": ["review"],
                    }
                ],
            },
        )
        peer = registered_peer.select_card(snapshot.id)
        correlation = RemoteTaskCorrelation.prepare(
            tenant_id=self._tenant_id,
            task_id=task.id,
            run_id=review_run.id,
            peer_id=peer.id,
            card_snapshot_id=snapshot.id,
            card_digest=snapshot.digest,
            endpoint_url="https://review.example/a2a/v1",
            protocol_binding="HTTP+JSON",
            protocol_version="1.0",
            endpoint_tenant=None,
            outbound_message_id=uuid4(),
            request_digest=f"sha256:{'3' * 64}",
        ).mark_sending()
        correlation = correlation.wait_remote(
            remote_task_id="remote-review-1",
            remote_context_id="research-brief",
            remote_state="working",
            response_digest=f"sha256:{'4' * 64}",
            from_poll=False,
            next_poll_at=utc_now() + timedelta(minutes=1),
        )

        approval = GovernedAction.create(
            tenant_id=self._tenant_id,
            requester_id="demo-reviewer",
            action_type=GovernedActionType.A2A_DELEGATE,
            resource_type="task",
            resource_id=task.id,
            arguments={"peer_id": str(peer.id), "purpose": "evidence-review"},
            policy_result=PolicyResult.REQUIRE_APPROVAL,
            reason_code="external_review_requires_approval",
            policy_bundle="showcase",
            policy_version="1",
            created_at=now,
            expires_at=now + timedelta(hours=1),
        ).decide(
            approver_id="showcase-approver",
            outcome=ApprovalOutcome.APPROVE,
            now=utc_now(),
        )

        task = aggregate.task
        task.start_coordination()
        with self._uow_factory() as uow:
            uow.tasks.save(task)
            for subtask in (research, analysis, review):
                uow.subtasks.save(subtask)
            for run in (research_run, analysis_run, review_run):
                uow.runs.add(run)
            uow.flush()
            uow.attempts.add(failed_attempt)
            uow.attempts.add(successful_attempt)
            uow.handoffs.add(handoff)
            uow.tool_invocations.add(invocation)
            uow.policy.add_action(approval)
            uow.a2a_registry.add_peer(registered_peer)
            uow.flush()
            uow.a2a_registry.add_snapshot(snapshot)
            uow.flush()
            uow.a2a_registry.save_peer(peer)
            uow.remote_correlations.add(correlation)
            uow.commit()

        return ShowcaseResult(task_id=str(task.id), objective=objective, interaction_count=10)

    @staticmethod
    def _spec(
        key: str,
        objective: str,
        agent_id: str,
        *,
        depends_on: tuple[str, ...] = (),
    ) -> SubtaskSpec:
        return SubtaskSpec.create(
            key=key,
            objective=objective,
            input={"role": key.title()},
            required_capabilities=("general.task",),
            depends_on=depends_on,
            preferred_agent_id=agent_id,
        )
