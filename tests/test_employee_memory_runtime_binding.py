from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentmesh.application.memory_runtime_services import RuntimeMemoryService
from agentmesh.domain.coordination import Subtask, SubtaskStatus
from agentmesh.domain.deliverable_acceptance import ACCEPTANCE_POLICY_INPUT_KEY
from agentmesh.domain.organizational_memory import MemoryNamespaceType, MemoryStatus, MemoryType
from agentmesh.domain.registry import AgentVersionStatus
from agentmesh.domain.tasks import RunRole, RunStatus, TaskExecutionMode, TaskStatus
from agentmesh.features import FeatureGateSet
from tests.test_organizational_memory import _accept, _company, _policy, _propose


@pytest.fixture
def employee_memory(
    company_service, organizational_memory_service, task_service, registry_service, uow_factory
):
    company, unit = _company(company_service)
    policy = _policy(
        organizational_memory_service,
        company.id,
        readable=[f"company/{company.id}", "employee/*", "position/*", "project/*", "unit/*"],
        writable=[f"company/{company.id}", "employee/*", "position/*", "project/*", "unit/*"],
        extraction_enabled=True,
    )
    definition = next(
        value
        for value in uow_factory.store.agent_definitions.values()
        if value.name == "test-agent"
    )
    version = uow_factory.store.agent_versions[definition.default_version_id]
    other = registry_service.ensure_builtin_agent("other-employee")
    position = company_service.create_position(
        company.id,
        primary_unit_id=unit.id,
        key="lyricist",
        title="Lyricist",
        responsibility_contract={"outcomes": ["lyrics"]},
        memory_policy_id=policy.id,
    )
    other_position = company_service.create_position(
        company.id,
        primary_unit_id=unit.id,
        key="critic",
        title="Critic",
        responsibility_contract={"outcomes": ["critique"]},
    )
    appointment = company_service.appoint(
        company.id,
        position_id=position.id,
        agent_definition_id=definition.id,
        agent_version_id=version.id,
        appointed_by="owner",
        reason="Appoint trusted lyricist.",
    )
    company_service.appoint(
        company.id,
        position_id=other_position.id,
        agent_definition_id=other.definition.id,
        agent_version_id=other.definition.default_version_id,
        appointed_by="owner",
        reason="Appoint independent critic.",
    )
    aggregate = task_service.create_task(
        "Write lyrics with company guidance",
        {"company_context": {"company_id": str(company.id)}},
    )
    aggregate = task_service.request_run(aggregate.task.id)
    run = aggregate.runs[0]
    runtime = RuntimeMemoryService(
        uow_factory=uow_factory,
        memory_service=organizational_memory_service,
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config(
            "full", "company_model=true,organizational_memory=true"
        ),
    )
    return SimpleNamespace(
        company=company,
        unit=unit,
        policy=policy,
        definition=definition,
        version=version,
        other=other.definition,
        position=position,
        other_position=other_position,
        appointment=appointment,
        task=aggregate.task,
        run=run,
        runtime=runtime,
        memory=organizational_memory_service,
        company_service=company_service,
        uow_factory=uow_factory,
    )


def _scopes(value):
    result = value.runtime._runtime_scope(value.task, value.run)
    assert result is not None
    return result[2]


def test_employee_and_position_are_bound_without_caller_workforce(employee_memory):
    value = employee_memory
    scopes = _scopes(value)
    assert (MemoryNamespaceType.EMPLOYEE, str(value.definition.id)) in scopes
    assert (MemoryNamespaceType.POSITION, str(value.position.id)) in scopes
    assert (MemoryNamespaceType.EMPLOYEE, str(value.other.id)) not in scopes


def test_caller_cannot_spoof_employee_definition(employee_memory):
    value = employee_memory
    value.task.input["company_context"]["workforce"] = [
        {"agent_name": value.run.agent_id, "agent_definition_id": str(value.other.id)}
    ]
    scopes = _scopes(value)
    assert (MemoryNamespaceType.EMPLOYEE, str(value.definition.id)) in scopes
    assert (MemoryNamespaceType.EMPLOYEE, str(value.other.id)) not in scopes


def test_caller_cannot_select_another_employees_position(employee_memory):
    value = employee_memory
    value.task.input["company_context"]["workforce"] = [
        {"agent_name": value.run.agent_id, "position_id": str(value.other_position.id)}
    ]
    scopes = _scopes(value)
    assert not any(
        kind in {MemoryNamespaceType.EMPLOYEE, MemoryNamespaceType.POSITION} for kind, _ in scopes
    )
    assert (MemoryNamespaceType.COMPANY, str(value.company.id)) in scopes


@pytest.mark.parametrize("invalid", ["unknown_version", "wrong_digest", "revoked", "wrong_name"])
def test_invalid_pinned_identity_never_opens_private_scope(employee_memory, invalid):
    value = employee_memory
    if invalid == "unknown_version":
        value.run.agent_version_id = uuid4()
    elif invalid == "wrong_digest":
        value.run.agent_version_digest = "0" * 64
    elif invalid == "revoked":
        value.uow_factory.store.agent_versions[value.version.id].status = AgentVersionStatus.REVOKED
    else:
        value.run.agent_id = value.other.name
    assert not any(
        kind in {MemoryNamespaceType.EMPLOYEE, MemoryNamespaceType.POSITION}
        for kind, _ in _scopes(value)
    )


def test_ended_appointment_revokes_employee_and_position_scope(employee_memory):
    value = employee_memory
    value.company_service.end_appointment(value.company.id, value.appointment.id)
    assert not any(
        kind in {MemoryNamespaceType.EMPLOYEE, MemoryNamespaceType.POSITION}
        for kind, _ in _scopes(value)
    )


def test_registry_default_change_does_not_change_pinned_employee_scope(employee_memory):
    value = employee_memory
    value.uow_factory.store.agent_definitions[value.definition.id].default_version_id = uuid4()
    assert (MemoryNamespaceType.EMPLOYEE, str(value.definition.id)) in _scopes(value)


def test_reading_employee_memory_also_keeps_shared_company_knowledge(employee_memory):
    value = employee_memory
    own = _propose(
        value.memory,
        value.company.id,
        value.policy.id,
        "Write lyrics with concise imagery.",
        namespace_type=MemoryNamespaceType.EMPLOYEE,
        namespace_id=str(value.definition.id),
    )
    other = _propose(
        value.memory,
        value.company.id,
        value.policy.id,
        "Write lyrics with private critique.",
        namespace_type=MemoryNamespaceType.EMPLOYEE,
        namespace_id=str(value.other.id),
    )
    shared = _propose(
        value.memory, value.company.id, value.policy.id, "Company guidance: avoid imitation."
    )
    for memory in (own, other, shared):
        _accept(value.memory, value.company.id, value.policy.id, memory.memory.id)
    assembly = value.runtime.assemble(value.task, value.run, None)
    ids = {item["memory_id"] for item in assembly.work_item.input["agentmesh_memory"]["records"]}
    assert str(own.memory.id) in ids
    assert str(shared.memory.id) in ids
    assert str(other.memory.id) not in ids


@pytest.mark.parametrize("namespace", ["EMPLOYEE", "POSITION", "UNIT", "USER"])
def test_wildcard_policy_cannot_write_unbound_automatic_candidate(employee_memory, namespace):
    value = employee_memory
    value.task.status = TaskStatus.COMPLETED
    value.task.current_run_id = value.run.id
    value.task.output = {
        "summary": "Completed lyrics",
        "memory_candidates": [
            {
                "memory_type": "PATTERN",
                "content": "Untrusted private learning.",
                "namespace_type": namespace,
                "namespace_id": str(uuid4()),
            }
        ],
    }
    value.run.status = RunStatus.SUCCEEDED
    value.uow_factory.store.runs[value.run.id] = deepcopy(value.run)
    with value.uow_factory() as uow:
        result = value.runtime.capture_completed_task_in_unit_of_work(uow, value.task)
        uow.commit()
    assert result.candidate_ids == ()
    assert result.rejected_count == 1


def test_trusted_employee_candidate_is_governed_and_attributed(employee_memory):
    value = employee_memory
    value.task.status = TaskStatus.COMPLETED
    value.task.current_run_id = value.run.id
    value.task.output = {
        "summary": "Completed lyrics",
        "memory_candidates": [
            {
                "memory_type": "PATTERN",
                "content": "Keep lyrics concise.",
                "namespace_type": "EMPLOYEE",
                "namespace_id": str(value.definition.id),
            }
        ],
    }
    value.run.status = RunStatus.SUCCEEDED
    value.uow_factory.store.runs[value.run.id] = deepcopy(value.run)
    with value.uow_factory() as uow:
        result = value.runtime.capture_completed_task_in_unit_of_work(uow, value.task)
        uow.commit()
    assert len(result.candidate_ids) == 1
    memory = value.memory.list_candidates(value.company.id)[0].memory
    assert memory.namespace_id == str(value.definition.id)
    assert memory.proposed_by_run_id == value.run.id


def test_reviewer_is_not_automatically_given_memory(employee_memory):
    value = employee_memory
    value.run.role = RunRole.REVIEWER
    assert value.runtime.assemble(value.task, value.run, None).search is None


def test_wrong_task_binding_or_tenant_cannot_assemble_memory(employee_memory):
    value = employee_memory
    value.run.task_id = uuid4()
    assert value.runtime.assemble(value.task, value.run, None).search is None


def _completed_work_item(value, *, other=False):
    run = deepcopy(value.run)
    run.id = uuid4()
    run.subtask_id = uuid4()
    run.status = RunStatus.SUCCEEDED
    definition = value.other if other else value.definition
    version = value.uow_factory.store.agent_versions[definition.default_version_id]
    run.agent_id = definition.name
    run.agent_version_id = version.id
    run.agent_version_digest = version.content_digest
    run.output = {
        "summary": "Completed work item",
        "memory_candidates": [
            {
                "memory_type": "PATTERN",
                "content": "Critic checks audio evidence."
                if other
                else "Lyricist uses clear imagery.",
                "namespace_type": "EMPLOYEE",
                "namespace_id": str(definition.id),
            }
        ],
    }
    subtask = Subtask.create(
        subtask_id=run.subtask_id,
        task_id=value.task.id,
        key="critique" if other else "lyrics",
        objective="Complete isolated employee work",
        input={},
        required_capabilities=(),
        preferred_agent_id=definition.name,
        initially_ready=True,
    )
    subtask.status = SubtaskStatus.COMPLETED
    subtask.current_run_id = run.id
    subtask.output = deepcopy(run.output)
    value.uow_factory.store.runs[run.id] = run
    value.uow_factory.store.subtasks[subtask.id] = subtask
    value.task.status = TaskStatus.COMPLETED
    value.task.execution_mode = TaskExecutionMode.COORDINATED
    value.task.current_run_id = None
    value.task.output = {"summary": "Company completed music task"}
    return run, subtask


def _capture(value):
    with value.uow_factory() as uow:
        result = value.runtime.capture_completed_task_in_unit_of_work(uow, value.task)
        uow.commit()
    return result


def test_completed_coordinated_task_collects_each_employees_own_learning(employee_memory):
    value = employee_memory
    lyricist, _ = _completed_work_item(value)
    critic, _ = _completed_work_item(value, other=True)
    result = _capture(value)
    assert len(result.candidate_ids) == 2
    candidates = value.memory.list_candidates(value.company.id)
    assert {item.memory.proposed_by_run_id for item in candidates} == {lyricist.id, critic.id}
    assert {item.memory.namespace_id for item in candidates} == {
        str(value.definition.id),
        str(value.other.id),
    }
    for item in candidates:
        assert item.memory.status is MemoryStatus.CANDIDATE
        assert item.evidence[0].evidence_type == "run-output"
        assert item.evidence[0].evidence_id == str(item.memory.proposed_by_run_id)
        assert len(item.evidence[0].evidence_digest) == 64


@pytest.mark.parametrize("overall", [TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELED])
def test_employee_learning_waits_for_overall_task_completion(employee_memory, overall):
    value = employee_memory
    _completed_work_item(value)
    value.task.status = overall
    assert _capture(value).candidate_ids == ()
    assert value.memory.list_candidates(value.company.id) == []


@pytest.mark.parametrize("bad", ["failed_run", "wrong_task", "wrong_subtask", "wrong_output"])
def test_only_matching_successful_winning_run_is_learning_source(employee_memory, bad):
    value = employee_memory
    run, subtask = _completed_work_item(value)
    if bad == "failed_run":
        run.status = RunStatus.FAILED
    elif bad == "wrong_task":
        run.task_id = uuid4()
    elif bad == "wrong_subtask":
        run.subtask_id = uuid4()
    else:
        subtask.output["summary"] = "Output does not match winning Run"
    value.task.current_run_id = run.id
    value.task.output = deepcopy(run.output)
    assert _capture(value).candidate_ids == ()


def test_superseded_successful_retry_does_not_propose_learning(employee_memory):
    value = employee_memory
    winning, _ = _completed_work_item(value)
    discarded = deepcopy(winning)
    discarded.id = uuid4()
    discarded.output["memory_candidates"][0]["content"] = "Discarded attempt must not be learned."
    value.uow_factory.store.runs[discarded.id] = discarded
    assert len(_capture(value).candidate_ids) == 1
    assert value.memory.list_candidates(value.company.id)[0].memory.proposed_by_run_id == winning.id


def test_subtask_learning_requires_review_even_with_auto_accept_policy(employee_memory):
    value = employee_memory
    _completed_work_item(value)
    value.uow_factory.store.memory_policies[value.policy.id].auto_accept_memory_types = [
        MemoryType.PATTERN
    ]
    assert len(_capture(value).candidate_ids) == 1
    memory = value.memory.list_candidates(value.company.id)[0].memory
    assert memory.status is MemoryStatus.CANDIDATE
    assert memory.reviewed_by is None


def test_completion_replay_does_not_recreate_rejected_personal_learning(employee_memory):
    value = employee_memory
    _completed_work_item(value)
    first = _capture(value)
    value.memory.review(
        value.company.id,
        first.candidate_ids[0],
        policy_id=value.policy.id,
        decision="REJECT",
        reviewer="owner",
        reviewer_roles={"TENANT_ADMIN"},
        reason="This learning is not reliable.",
    )
    second = _capture(value)
    assert second.candidate_ids == first.candidate_ids
    assert second.rejected_count == 0
    with value.uow_factory() as uow:
        records = uow.organizational_memory.list_records(value.company.id)
    assert len(records) == 1
    assert records[0].status is MemoryStatus.REJECTED


def test_coordinated_final_run_does_not_duplicate_subtask_learning(employee_memory):
    value = employee_memory
    run, _ = _completed_work_item(value)
    value.task.current_run_id = run.id
    value.task.output = deepcopy(run.output)
    assert len(_capture(value).candidate_ids) == 1


def test_coordinated_employee_learning_waits_for_deliverable_acceptance(employee_memory):
    value = employee_memory
    _completed_work_item(value)
    value.task.input[ACCEPTANCE_POLICY_INPUT_KEY] = {
        "version": 1,
        "target_subtask_key": "lyrics",
        "require_human_review": True,
        "checks": [
            {
                "key": "summary",
                "description": "Summary must exist",
                "kind": "OUTPUT_PATH_EXISTS",
                "path": ["summary"],
                "required": True,
            }
        ],
    }
    assert _capture(value).candidate_ids == ()
    assert value.memory.list_candidates(value.company.id) == []
    value.task.input[ACCEPTANCE_POLICY_INPUT_KEY]["require_human_review"] = False
    assert len(_capture(value).candidate_ids) == 1
    value.run.task_id = value.task.id
    value.task.tenant_id = "another-tenant"
    assert value.runtime.assemble(value.task, value.run, None).search is None
