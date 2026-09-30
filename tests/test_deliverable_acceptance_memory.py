from types import SimpleNamespace
from unittest.mock import Mock

from agentmesh.application.memory_runtime_services import RuntimeMemoryService
from agentmesh.domain.deliverable_acceptance import ACCEPTANCE_POLICY_INPUT_KEY
from agentmesh.domain.tasks import TaskStatus
from agentmesh.features import FeatureGateSet


def test_unaccepted_completed_task_does_not_extract_memory(monkeypatch):
    import agentmesh.application.memory_runtime_services as module

    monkeypatch.setattr(
        module, "project_deliverable_acceptance", lambda aggregate: {"delivery_allowed": False}
    )
    runtime = RuntimeMemoryService(
        uow_factory=Mock(),
        memory_service=Mock(),
        tenant_id="tenant",
        feature_gates=FeatureGateSet.from_config(
            "full", "company_model=true,organizational_memory=true"
        ),
    )
    task = SimpleNamespace(
        id="task",
        tenant_id="tenant",
        status=TaskStatus.COMPLETED,
        output={"memory_candidates": [{"content": "Unverified fact"}]},
        input={ACCEPTANCE_POLICY_INPUT_KEY: {"version": 1}},
    )
    uow = Mock()
    result = runtime.capture_completed_task_in_unit_of_work(uow, task)
    assert result.candidate_ids == ()
    uow.runs.list_for_task.assert_not_called()
    runtime._memory_service.propose_in_unit_of_work.assert_not_called()
