"""No-network checks for the opt-in Feishu notification boundary."""

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from agentmesh.config import Settings
from agentmesh.entrypoints.feishu_notifier import validate_configuration
from agentmesh.features import Feature, FeatureGateSet
from agentmesh.infrastructure.postgres.models import GovernedActionRecord, TaskRecord
from agentmesh.integrations.feishu_notifications import (
    ClaimedNotification,
    FeishuClient,
    FeishuNotificationWorker,
    build_card,
)


def _notification(kind: str = "COMPLETED") -> ClaimedNotification:
    return ClaimedNotification(uuid4(), "tenant", "TASK", uuid4(), kind, 1)


def test_gate_is_explicit_opt_in_even_in_full_profile() -> None:
    assert not FeatureGateSet.from_config("full").is_enabled(Feature.FEISHU_NOTIFICATIONS)
    assert FeatureGateSet.from_config(
        "minimal", "feishu_notifications=true"
    ).is_enabled(Feature.FEISHU_NOTIFICATIONS)


def test_notifier_rejects_missing_configuration_and_insecure_link() -> None:
    with pytest.raises(Exception, match="feishu_notifications"):
        validate_configuration(Settings(feature_profile="minimal"))
    with pytest.raises(ValueError, match="APP_ID"):
        validate_configuration(Settings(feature_gates="feishu_notifications=true"))
    with pytest.raises(ValueError, match="HTTPS"):
        validate_configuration(
            Settings(
                feature_gates="feishu_notifications=true",
                feishu_app_id="cli_test",
                feishu_app_secret="test-secret",
                feishu_chat_id="oc_test",
                feishu_task_base_url="http://example.com",
            )
        )


def test_card_hides_task_content_by_default_and_links_to_exact_task() -> None:
    notification = _notification()
    task = TaskRecord(
        id=notification.subject_id,
        objective="confidential objective",
        output={"summary": "confidential result"},
        error=None,
    )
    card = build_card(
        notification, task, task_base_url="https://mesh.example", include_content=False
    )
    rendered = str(card)
    assert "confidential" not in rendered
    assert f"https://mesh.example/?task={notification.subject_id}" in rendered
    with_content = build_card(
        notification, task, task_base_url=None, include_content=True
    )
    assert "confidential result" in str(with_content)


def test_feishu_client_uses_app_token_and_stable_delivery_uuid() -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/tenant_access_token/internal"):
            assert b"test-secret" in request.content
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "token", "expire": 7200}
            )
        assert request.headers["authorization"] == "Bearer token"
        assert request.url.params["receive_id_type"] == "chat_id"
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_test"}})

    transport = httpx.MockTransport(respond)
    with httpx.Client(transport=transport) as http:
        client = FeishuClient(
            app_id="cli_test", app_secret="test-secret", chat_id="oc_test", http_client=http
        )
        notification_id = uuid4()
        client.send(notification_id=notification_id, card={"elements": []})
        client.send(notification_id=notification_id, card={"elements": []})
    assert len(calls) == 3  # Token reused.
    assert all(request.read() for request in calls)
    assert str(notification_id) in calls[1].content.decode()


def test_configured_acceptance_suppresses_unverified_completion_summary() -> None:
    notification = _notification()
    task = TaskRecord(
        id=notification.subject_id,
        objective="Evaluate candidates",
        input={"agentmesh_deliverable_acceptance": {"version": 1}},
        output={"summary": "Unverified business claim"},
        error=None,
    )
    rendered = str(build_card(notification, task, task_base_url=None, include_content=True))
    assert "Unverified business claim" not in rendered
    assert "独立验收" in rendered


def test_worker_retries_failure_without_mutating_task() -> None:
    notification = _notification()
    task = SimpleNamespace(
        id=notification.subject_id, status="COMPLETED", objective="test", output={}, error=None
    )

    class Store:
        def __init__(self) -> None:
            self.finished = []
            self.failed = []

        def claim(self, *, worker_id: str):
            return [notification]

        def subject(self, claimed):
            return task

        def finish(self, claimed, *, worker_id: str, status: str):
            self.finished.append(status)

        def fail(self, claimed, *, worker_id: str, error: str):
            self.failed.append(error)

    class Client:
        def send(self, *, notification_id, card):
            raise RuntimeError("temporary failure")

    store = Store()
    worker = FeishuNotificationWorker(
        worker_id="test", store=store, client=Client(), task_base_url=None,
        include_content=False,
    )
    assert worker.run_once() == 1
    assert store.finished == []
    assert store.failed == ["RuntimeError: temporary failure"]
    assert task.status == "COMPLETED"


def test_worker_skips_stale_approval_notification() -> None:
    notification = _notification("WAITING_APPROVAL")

    class Store:
        status = None

        def claim(self, *, worker_id: str):
            return [notification]

        def subject(self, claimed):
            return SimpleNamespace(status="COMPLETED")

        def finish(self, claimed, *, worker_id: str, status: str):
            self.status = status

    class Client:
        def send(self, **kwargs):
            pytest.fail("A stale approval must never reach Feishu")

    store = Store()
    worker = FeishuNotificationWorker(
        worker_id="test", store=store, client=Client(), task_base_url=None,
        include_content=False,
    )
    assert worker.run_once() == 1
    assert store.status == "SKIPPED"


def test_governed_approval_card_links_to_approval_view_without_arguments() -> None:
    notification = ClaimedNotification(
        uuid4(), "tenant", "GOVERNED_ACTION", uuid4(), "PENDING_APPROVAL", 1
    )
    action = GovernedActionRecord(
        id=notification.subject_id,
        action_type="MCP_WRITE",
        resource_type="tool",
        arguments={"secret": "never send this"},
    )
    card = build_card(
        notification, action, task_base_url="https://mesh.example", include_content=True
    )
    assert f"https://mesh.example/?approval={action.id}" in str(card)
    assert "never send this" not in str(card)


def test_worker_skips_expired_governed_approval() -> None:
    from datetime import datetime, timedelta, timezone

    notification = ClaimedNotification(
        uuid4(), "tenant", "GOVERNED_ACTION", uuid4(), "PENDING_APPROVAL", 1
    )

    class Store:
        status = None

        def claim(self, *, worker_id: str):
            return [notification]

        def subject(self, claimed):
            return SimpleNamespace(
                approval_status="PENDING",
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )

        def finish(self, claimed, *, worker_id: str, status: str):
            self.status = status

    class Client:
        def send(self, **kwargs):
            pytest.fail("An expired approval must never reach Feishu")

    store = Store()
    worker = FeishuNotificationWorker(
        worker_id="test", store=store, client=Client(), task_base_url=None,
        include_content=False,
    )
    assert worker.run_once() == 1
    assert store.status == "SKIPPED"
