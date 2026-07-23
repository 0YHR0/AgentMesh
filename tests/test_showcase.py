from uuid import UUID

from agentmesh.application.showcase_services import ResearchBriefShowcaseService
from agentmesh.bootstrap import ApplicationContainer
from agentmesh.domain.tasks import AttemptStatus, TaskStatus
from tests.fakes import InMemoryUnitOfWorkFactory


def test_research_brief_showcase_projects_every_governed_transport(
    application_container: ApplicationContainer,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    result = ResearchBriefShowcaseService(
        task_service=application_container.task_service,
        planning_service=application_container.planning_service,
        uow_factory=uow_factory,
        tenant_id="test-tenant",
    ).create()
    task = application_container.task_service.get_task(UUID(result.task_id))
    interactions = application_container.activity_service.interactions(task.task.id, limit=100)

    assert result.interaction_count == 10
    assert task.task.status is TaskStatus.RUNNING
    assert {subtask.key for subtask in task.subtasks} == {
        "research",
        "analysis",
        "review",
        "publish",
    }
    assert {attempt.status for attempt in task.attempts} == {
        AttemptStatus.FAILED,
        AttemptStatus.SUCCEEDED,
    }
    assert {event.transport for event in interactions} == {
        "HANDOFF",
        "MCP",
        "A2A",
        "POLICY",
        "PLAN_PATCH",
    }
    assert len(interactions) == result.interaction_count
