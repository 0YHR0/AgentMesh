"""Independent sender identity must use the pinned Run employee, not card text or role labels."""

import json
import os
import traceback
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentmesh.config import Settings
from agentmesh.entrypoints.feishu_notifier import build_employee_clients, validate_configuration
from agentmesh.integrations.feishu_identities import load_employee_bot_credentials
from agentmesh.integrations.feishu_notifications import (
    ClaimedNotification,
    CollaborationSubject,
    FeishuNotificationWorker,
)


def config():
    return {
        "schema_version": 1, "tenant_id": "tenant", "chat_id": "oc_group",
        "bots": [{"agent_id": "employee-one", "app_id": "cli_one", "app_secret": "test-secret"}],
    }


def write_config(tmp_path, value):
    path = tmp_path / "bots.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return str(path)


def test_read_valid_bounded_private_file_and_redacted_credentials(tmp_path):
    bots = load_employee_bot_credentials(
        write_config(tmp_path, config()), tenant_id="tenant", chat_id="oc_group"
    )
    assert bots["employee-one"].app_secret.get_secret_value() == "test-secret"
    assert "test-secret" not in repr(bots)


@pytest.mark.parametrize("change", [
    lambda v: v.update(schema_version=True),
    lambda v: v.update(schema_version=2),
    lambda v: v.update(tenant_id="other"),
    lambda v: v.update(chat_id="oc_other"),
    lambda v: v.update(bots=[]),
    lambda v: v.update(endpoint="https://attacker.invalid"),
    lambda v: v["bots"][0].update(agent_id=" employee-one"),
    lambda v: v["bots"][0].update(agent_id="employee\u200bone"),
    lambda v: v["bots"][0].update(app_secret=""),
    lambda v: v["bots"][0].update(app_secret="credential with whitespace"),
    lambda v: v["bots"][0].update(app_id="not-an-app"),
    lambda v: v["bots"].append(dict(v["bots"][0])),
    lambda v: v["bots"].append(dict(v["bots"][0], agent_id="employee-two")),
])
def test_invalid_files_fail_without_echoing_secrets(tmp_path, change):
    value = config()
    change(value)
    with pytest.raises(ValueError, match="credential details withheld") as caught:
        load_employee_bot_credentials(
            write_config(tmp_path, value), tenant_id="tenant", chat_id="oc_group"
        )
    rendered = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
    assert "test-secret" not in rendered


@pytest.mark.parametrize(
    "data", ['{"schema_version":1,"schema_version":1}', "[", "x" * 65537],
    ids=["duplicate-keys", "invalid-json", "oversize"],
)
def test_duplicate_fields_malformed_and_oversize_files_rejected(tmp_path, data):
    path = tmp_path / "bots.json"
    path.write_text(data)
    path.chmod(0o600)
    with pytest.raises(ValueError, match="credential details withheld"):
        load_employee_bot_credentials(str(path), tenant_id="tenant", chat_id="oc_group")


@pytest.mark.skipif(os.name == "nt", reason="Windows requires operator-managed ACLs")
def test_broad_posix_permissions_rejected(tmp_path):
    path = write_config(tmp_path, config())
    os.chmod(path, 0o644)
    with pytest.raises(ValueError):
        load_employee_bot_credentials(path, tenant_id="tenant", chat_id="oc_group")


def settings(**changes):
    return Settings(_env_file=None, **dict({
        "feature_gates": "feishu_notifications=true", "tenant_id": "tenant",
        "feishu_app_id": "cli_summary", "feishu_app_secret": "summary-secret",
        "feishu_chat_id": "oc_group", "feishu_sync_collaboration": True,
    }, **changes))


def test_identity_opt_in_off_does_not_read_file(tmp_path):
    assert not Settings(_env_file=None).feishu_employee_bots_enabled
    assert build_employee_clients(settings(feishu_employee_bots_file="missing")) == {}


def test_enabled_routing_requires_sync_and_file():
    with pytest.raises(ValueError, match="SYNC_COLLABORATION"):
        validate_configuration(settings(
            feishu_employee_bots_enabled=True, feishu_sync_collaboration=False
        ))
    with pytest.raises(ValueError, match="BOTS_FILE"):
        validate_configuration(settings(feishu_employee_bots_enabled=True))


def test_clients_share_configured_group_but_not_tokens(tmp_path):
    value = config()
    value["bots"].append({
        "agent_id": "employee-two", "app_id": "cli_two", "app_secret": "second-secret",
    })
    clients = build_employee_clients(settings(
        feishu_employee_bots_enabled=True, feishu_employee_bots_file=write_config(tmp_path, value)
    ))
    try:
        assert set(clients) == {"employee-one", "employee-two"}
        assert clients["employee-one"] is not clients["employee-two"]
        assert all(client._chat_id == "oc_group" for client in clients.values())
    finally:
        for client in clients.values():
            client.close()


def test_employee_must_not_reuse_summary_app(tmp_path):
    value = config()
    value["bots"][0]["app_id"] = "cli_summary"
    with pytest.raises(ValueError, match="distinct"):
        build_employee_clients(settings(
            feishu_employee_bots_enabled=True,
            feishu_employee_bots_file=write_config(tmp_path, value)
        ))


class Client:
    def __init__(self, *, fail=False):
        self.sent = []
        self.failed = fail

    def send(self, **kwargs):
        self.sent.append(kwargs)
        if self.failed:
            raise RuntimeError("Feishu API code 230002")


class Store:
    def __init__(self, kind="COLLAB_RESULT", agent="employee-one"):
        self.notification = ClaimedNotification(
            uuid4(), "tenant", "TASK_RUN" if kind.startswith("COLLAB") else "TASK",
            uuid4(), kind, 1,
        )
        self.value = CollaborationSubject(
            uuid4(), self.notification.subject_id, agent, "role-is-not-an-identity", "Label",
            datetime.now(timezone.utc), "SUCCEEDED", (), "Public summary",
        ) if kind.startswith("COLLAB") else SimpleNamespace(
            status="COMPLETED", id=self.notification.subject_id,
            input={}, output={}, objective="test"
        )
        self.finished = []
        self.failed = []

    def claim(self, **kwargs):
        return [self.notification]

    def subject(self, notification):
        return self.value

    def finish(self, notification, **kwargs):
        self.finished.append(kwargs["status"])

    def fail(self, notification, **kwargs):
        self.failed.append(kwargs["error"])


def worker(store, summary, employees=None, fallback=True):
    return FeishuNotificationWorker(
        worker_id="test", store=store, client=summary, task_base_url=None, include_content=False,
        sync_collaboration=True, employee_clients=employees, employee_bot_fallback=fallback,
    )


@pytest.mark.parametrize("kind", ["COLLAB_STARTED", "COLLAB_RESULT", "COLLAB_FAILED"])
def test_routes_exact_employee_with_same_delivery_uuid(kind):
    store, summary, employee = Store(kind), Client(), Client()
    assert worker(store, summary, {"employee-one": employee}).run_once() == 1
    assert not summary.sent
    assert employee.sent[0]["notification_id"] == store.notification.id
    assert store.finished == ["DELIVERED"]


def test_mapped_failure_retries_without_impersonating_summary_bot():
    store, summary, employee = Store(), Client(), Client(fail=True)
    worker(store, summary, {"employee-one": employee}).run_once()
    assert employee.sent and not summary.sent
    assert store.failed == ["RuntimeError: Feishu API code 230002"]
    assert not store.finished


@pytest.mark.parametrize("fallback", [True, False])
def test_unmapped_identity_obeys_explicit_fallback_not_role_label(fallback):
    store, summary, employee = Store(agent="unknown"), Client(), Client()
    worker(store, summary, {"role-is-not-an-identity": employee}, fallback).run_once()
    assert not employee.sent
    assert bool(summary.sent) is fallback
    assert bool(store.failed) is not fallback


def test_task_summary_keeps_original_sender():
    store, summary, employee = Store("COMPLETED"), Client(), Client()
    worker(store, summary, {"employee-one": employee}, False).run_once()
    assert len(summary.sent) == 1 and not employee.sent


def test_empty_mapping_cannot_silently_bypass_strict_mode():
    with pytest.raises(ValueError, match="nonempty bindings"):
        worker(Store(), Client(), {}, False)
