from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentmesh.domain.business_activity import ACTIVITY_SCHEMA, BusinessActivityNotice
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.infrastructure.postgres.repositories import SqlAlchemyOutboxRepository
from agentmesh.integrations.feishu_notifications import (
    ClaimedNotification,
    FeishuNotificationWorker,
    build_card,
    project_business_activity,
)


def records(summary="Actual audio reviewed; revision recommended"):
    obj = SimpleNamespace(id=uuid4(), company_id=uuid4())
    company = SimpleNamespace(id=obj.company_id, tenant_id="tenant")
    revision = SimpleNamespace(object_id=obj.id, revision=3, data_digest="pinned-digest")
    notice = BusinessActivityNotice(
        agent_id="critic", role_label="Audio critic", phase="RESULT", summary=summary
    )
    envelope = MessageEnvelope.domain_event(
        schema_name=ACTIVITY_SCHEMA,
        tenant_id="tenant",
        aggregate_id=obj.id,
        payload={
            "company_id": str(company.id),
            "object_id": str(obj.id),
            "revision": 3,
            "data_digest": revision.data_digest,
            "activity": notice.model_dump(),
        },
    )
    event = SimpleNamespace(
        id=envelope.message_id,
        tenant_id="tenant",
        topic=ACTIVITY_SCHEMA,
        envelope=envelope.to_dict(),
    )
    notification = ClaimedNotification(
        uuid4(), "tenant", "BUSINESS_ACTIVITY", event.id, "ACTIVITY_RESULT", 1
    )
    return notification, event, obj, revision, company


def test_projection_binds_exact_revision_and_hides_content_by_default():
    notification, event, obj, revision, company = records()
    subject = project_business_activity(notification, event, obj, revision, company)
    assert subject and subject.revision == 3
    assert "Actual audio" not in str(
        build_card(notification, subject, task_base_url=None, include_content=False)
    )
    assert "Actual audio" in str(
        build_card(notification, subject, task_base_url=None, include_content=True)
    )


@pytest.mark.parametrize("change", ["tenant", "digest", "revision", "company", "event", "extra"])
def test_invalid_provenance_is_not_sent(change):
    notification, event, obj, revision, company = records()
    if change == "tenant":
        company.tenant_id = "another"
    elif change == "digest":
        revision.data_digest = "changed"
    elif change == "revision":
        revision.revision = 4
    elif change == "company":
        company.id = uuid4()
    elif change == "event":
        event.envelope["payload"]["activity"]["phase"] = "FAILED"
    else:
        event.envelope["payload"]["raw_memory"] = "must not leak"
    assert project_business_activity(notification, event, obj, revision, company) is None


def test_credential_summary_is_withheld_without_losing_status():
    args = records("api_key=very-sensitive-secret-value")
    subject = project_business_activity(*args)
    assert subject and subject.summary is None
    assert "sensitive" not in str(
        build_card(args[0], subject, task_base_url=None, include_content=True)
    )


def test_notice_rejects_raw_unbounded_payload():
    with pytest.raises(ValidationError):
        BusinessActivityNotice(
            agent_id="critic", role_label="Critic", phase="RESULT", summary="x" * 501
        )
    with pytest.raises(ValidationError):
        BusinessActivityNotice(
            agent_id="critic", role_label="Critic", phase="RESULT", raw_memory="secret"
        )


@pytest.mark.parametrize("enabled,expected", [(False, 1), (True, 2)])
def test_repository_queues_only_with_explicit_collaboration_gate(enabled, expected):
    args = records()
    session = SimpleNamespace(rows=[])
    session.add = session.rows.append
    repository = SqlAlchemyOutboxRepository(session, feishu_collaboration_enabled=enabled)
    repository.add(MessageEnvelope.from_dict(args[1].envelope))
    assert len(session.rows) == expected
    if enabled:
        job = session.rows[-1]
        assert job.subject_type == "BUSINESS_ACTIVITY" and job.event_kind == "ACTIVITY_RESULT"
        assert job.subject_revision == 3 and job.subject_id == args[1].id


@pytest.mark.parametrize("enabled,expected", [(False, "SKIPPED"), (True, "DELIVERED")])
def test_worker_routes_to_employee_or_skips_when_disabled(enabled, expected):
    args = records()
    subject = project_business_activity(*args)
    store = SimpleNamespace(status=None)
    store.claim = lambda **kwargs: [args[0]]
    store.subject = lambda notification: subject
    store.finish = lambda notification, **kwargs: setattr(store, "status", kwargs["status"])
    generic = SimpleNamespace(send=lambda **kwargs: pytest.fail("Must use employee bot"))
    employee = SimpleNamespace(sent=[])
    employee.send = lambda **kwargs: employee.sent.append(kwargs)
    worker = FeishuNotificationWorker(
        worker_id="test",
        store=store,
        client=generic,
        task_base_url=None,
        include_content=True,
        sync_collaboration=enabled,
        employee_clients={"critic": employee} if enabled else None,
        send_interval_seconds=0,
    )
    assert worker.run_once() == 1
    assert store.status == expected
    assert len(employee.sent) == int(enabled)


def test_business_revision_and_notice_share_transaction(
    company_service, business_object_service, uow_factory
):
    from tests.test_business_objects import _company, _lead_type

    company, position = _company(company_service)
    object_type = _lead_type(business_object_service, company.id)
    created = business_object_service.create_object(
        company.id,
        type_id=object_type.id,
        data={"name": "Ada", "email": "private@example.test"},
        owner_position_id=position.id,
        actor="owner",
    )
    notice = BusinessActivityNotice(
        agent_id="employee",
        role_label="Analyst",
        phase="RESULT",
        summary="Evidence-backed qualification complete",
    )
    changed = business_object_service.apply_action(
        company.id,
        created.object.id,
        action_key="qualify",
        expected_revision=1,
        input={"score": 95},
        actor="owner",
        evidence_refs=["artifact:test"],
        actor_position_key="sales-analyst",
        actor_capabilities=["crm.qualify"],
        activity=notice,
    )
    events = [item for item in uow_factory.store.outbox if item.schema_name == ACTIVITY_SCHEMA]
    assert len(events) == 1
    assert events[0].payload["revision"] == changed.object.current_revision == 2
    assert events[0].payload["data_digest"] == changed.revisions[-1].data_digest
    assert "private@example.test" not in str(events[0].payload)
    with pytest.raises(Exception, match="Stale"):
        business_object_service.apply_action(
            company.id,
            created.object.id,
            action_key="qualify",
            expected_revision=1,
            input={"score": 95},
            actor="owner",
            activity=notice,
        )
    assert (
        len([item for item in uow_factory.store.outbox if item.schema_name == ACTIVITY_SCHEMA]) == 1
    )
