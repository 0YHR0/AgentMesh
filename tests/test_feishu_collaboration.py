"""Project only real pinned work exchanges, not private context or imagined chat."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentmesh.infrastructure.postgres.models import SubtaskRecord, TaskRecord, TaskRunRecord
from agentmesh.integrations.feishu_notifications import (
    ClaimedNotification,
    FeishuNotificationWorker,
    build_card,
    project_collaboration_subject,
    safe_content,
)
from agentmesh.runtime_sdk.canonical import canonical_json_bytes


@pytest.fixture
def exchange():
    now = datetime.now(timezone.utc)
    task = TaskRecord(id=uuid4(), tenant_id="private", execution_mode="COORDINATED",
                      status="COMPLETED", input={"secret": "never-send-input"})
    source = SubtaskRecord(id=uuid4(), task_id=task.id, key="lyrics")
    target = SubtaskRecord(id=uuid4(), task_id=task.id, key="composition")
    upstream = TaskRunRecord(id=uuid4(), task_id=task.id, subtask_id=source.id,
                             agent_id="lyricist", status="SUCCEEDED")
    payload = {"summary": "歌词围绕夜归与灯光。", "lyrics": "private-full-lyrics",
               "memory_candidates": [{"content": "never-send-memory"}]}
    run = TaskRunRecord(id=uuid4(), task_id=task.id, subtask_id=target.id,
                       agent_id="composer", role="EXECUTOR", status="SUCCEEDED",
                       work_item_pinned_at=now, completed_at=now + timedelta(seconds=1),
                       output={"summary": "编曲使用已交接的歌词。",
                               "execution": "never-send-trace"})
    transfer = {
        "kind": "DEPENDENCY_RESULT", "source_subtask_id": str(source.id),
        "source_key": source.key, "source_run_id": str(upstream.id),
        "source_agent_id": upstream.agent_id, "target_subtask_id": str(target.id),
        "target_key": target.key, "target_run_id": str(run.id),
        "target_agent_id": run.agent_id, "payload": payload,
        "payload_sha256": sha256(canonical_json_bytes(payload)).hexdigest(),
    }
    run.work_item_snapshot = {
        "schema_version": 1,
        "work_item": {"objective": "never-send-full-prompt", "input": {
            "subtask_input": {"role": "编曲", "private": "never-send-context"},
            "dependency_outputs": {source.key: payload}, "accepted_handoffs": [],
            "organizational_memory": {"content": "never-send-private-memory"},
        }}, "transfers": [transfer],
    }
    records = {(SubtaskRecord, source.id): source, (TaskRunRecord, upstream.id): upstream}

    def project(kind="COLLAB_STARTED"):
        notice = ClaimedNotification(uuid4(), task.tenant_id, "TASK_RUN", run.id, kind, 1)
        return notice, project_collaboration_subject(
            notice, task=task, run=run, subtask=target,
            get_record=lambda model, key: records.get((model, key)),
        )

    return SimpleNamespace(task=task, source=source, target=target, upstream=upstream,
                           run=run, transfer=transfer, payload=payload, records=records,
                           project=project)


def test_real_delivery_card_only_selects_public_business_summary(exchange):
    notice, subject = exchange.project()
    assert subject is not None
    rendered = str(build_card(notice, subject, task_base_url=None, include_content=True))
    assert "lyricist / lyrics → composer / composition" in rendered
    assert "歌词围绕夜归与灯光" in rendered
    assert "内容指纹" in rendered
    assert "never-send" not in rendered
    assert "private-full-lyrics" not in rendered
    metadata = str(build_card(notice, subject, task_base_url=None, include_content=False))
    assert "歌词围绕夜归与灯光" not in metadata


def test_result_uses_succeeded_run_not_task_completed_and_is_provisional(exchange):
    notice, subject = exchange.project("COLLAB_RESULT")
    assert subject is not None and subject.status == "SUCCEEDED"
    card = str(build_card(notice, subject, task_base_url=None, include_content=True))
    assert "编曲使用已交接的歌词" in card
    assert "尚不代表最终交付或人工批准" in card
    exchange.target.output = {"summary": "wrong-latest-retry-output"}
    _, subject = exchange.project("COLLAB_RESULT")
    assert subject.output_summary == exchange.run.output["summary"]


def test_discussion_card_leads_with_real_reply_not_audit_metadata(exchange):
    role = exchange.run.work_item_snapshot["work_item"]["input"]["subtask_input"]
    role.update(collaboration_mode="discussion", discussion_topic="前奏与可唱性", discussion_turn=3)
    exchange.run.output["summary"] = "词作人，我不同意整段删除；可以保留热汤意象，压缩其他句。"
    notice, subject = exchange.project("COLLAB_RESULT")
    card = build_card(notice, subject, task_base_url=None, include_content=True)
    rendered = str(card)
    assert "我不同意整段删除" in rendered
    assert "创作讨论" in rendered and "不是内部推理或主人批准" in rendered
    assert "never-send" not in rendered
    assert "阶段业务摘要" not in rendered
    hidden = str(build_card(notice, subject, task_base_url=None, include_content=False))
    assert "我不同意整段删除" not in hidden


def test_discussion_mode_does_not_export_secret_topic(exchange):
    role = exchange.run.work_item_snapshot["work_item"]["input"]["subtask_input"]
    role.update(collaboration_mode="discussion",
                discussion_topic="Bearer private-credential", discussion_turn=True)
    _, subject = exchange.project("COLLAB_RESULT")
    assert subject.discussion_topic is None and subject.discussion_turn is None


@pytest.mark.parametrize("status", ["FAILED", "CANCELED"])
def test_failed_run_does_not_export_error_body(exchange, status):
    exchange.run.status = status
    exchange.run.error = "private provider response and token"
    notice, subject = exchange.project("COLLAB_FAILED")
    assert subject is not None and subject.status == status
    assert "private provider" not in str(build_card(
        notice, subject, task_base_url=None, include_content=True,
    ))


@pytest.mark.parametrize("mutation", [
    "tenant", "task", "subtask", "supervisor", "schema", "digest", "source_task",
    "source_run", "target_agent", "unpinned", "naive_timestamp", "missing_payload",
])
def test_projection_fails_closed_on_untrusted_binding_or_evidence(exchange, mutation):
    if mutation == "tenant":
        exchange.task.tenant_id = "other"
        notice = ClaimedNotification(uuid4(), "private", "TASK_RUN", exchange.run.id,
                                     "COLLAB_STARTED", 1)
        assert project_collaboration_subject(
            notice, task=exchange.task, run=exchange.run, subtask=exchange.target,
            get_record=lambda model, key: exchange.records.get((model, key)),
        ) is None
        return
    if mutation == "task":
        exchange.run.task_id = uuid4()
    elif mutation == "subtask":
        exchange.target.task_id = uuid4()
    elif mutation == "supervisor":
        exchange.run.role = "SUPERVISOR"
    elif mutation == "schema":
        exchange.run.work_item_snapshot["schema_version"] = True
    elif mutation == "digest":
        exchange.transfer["payload_sha256"] = "0" * 64
    elif mutation == "source_task":
        exchange.source.task_id = uuid4()
    elif mutation == "source_run":
        exchange.upstream.agent_id = "wrong-agent"
    elif mutation == "target_agent":
        exchange.transfer["target_agent_id"] = "wrong-agent"
    elif mutation == "unpinned":
        exchange.run.work_item_pinned_at = None
    elif mutation == "naive_timestamp":
        exchange.run.work_item_pinned_at = datetime.now()
    elif mutation == "missing_payload":
        del exchange.transfer["payload"]
    assert exchange.project()[1] is None


@pytest.mark.parametrize("value", [
    "sk-test-secret", "mpg-test-secret", "AQ.example_secret", "Bearer secret",
    "api_key=secret", "https://example.com/?access_token=secret",
    "https://user:secret@example.com", "https://example.com/?%61pi_key=secret",
    "safe text " + "x" * 501, "\ud800",
])
def test_credential_surfaces_and_oversized_text_are_withheld(value):
    assert safe_content(value) is None


def test_control_characters_removed_and_plain_text_escapes_chat_mentions():
    assert safe_content("夜\u200b归\x00的灯") == "夜归的灯"


def test_worker_delivers_historic_start_and_result_after_task_completed(exchange):
    items = [exchange.project(kind) for kind in ("COLLAB_STARTED", "COLLAB_RESULT")]
    finished, sent, slept = [], [], []
    subjects = {notice.id: subject for notice, subject in items}
    store = SimpleNamespace(
        claim=lambda **kwargs: [notice for notice, _ in items],
        subject=lambda notice: subjects[notice.id],
        finish=lambda notice, **kwargs: finished.append(kwargs["status"]),
        fail=lambda *args, **kwargs: pytest.fail("delivery should succeed"),
    )
    client = SimpleNamespace(send=lambda **kwargs: sent.append(kwargs))
    worker = FeishuNotificationWorker(worker_id="test", store=store, client=client,
                                      task_base_url=None, include_content=True,
                                      sync_collaboration=True, sleep=slept.append)
    assert worker.run_once() == 2
    assert finished == ["DELIVERED", "DELIVERED"]
    assert len(sent) == 2 and len(slept) == 1
    assert [value["notification_id"] for value in sent] == [notice.id for notice, _ in items]


def test_worker_skips_collaboration_when_extra_opt_in_is_off(exchange):
    notice, subject = exchange.project()
    finished = []
    store = SimpleNamespace(
        claim=lambda **kwargs: [notice], subject=lambda value: subject,
        finish=lambda value, **kwargs: finished.append(kwargs["status"]),
    )
    worker = FeishuNotificationWorker(
        worker_id="test", store=store,
        client=SimpleNamespace(send=lambda **kwargs: pytest.fail("no egress")),
        task_base_url=None, include_content=True,
    )
    assert worker.run_once() == 1 and finished == ["SKIPPED"]


def test_entire_transfer_list_checked_not_only_displayed_prefix(exchange):
    snapshot = exchange.run.work_item_snapshot
    snapshot["transfers"] = [deepcopy(exchange.transfer) for _ in range(21)]
    snapshot["transfers"][-1]["payload_sha256"] = "0" * 64
    assert exchange.project()[1] is None


def test_accepted_handoff_uses_pinned_summary_not_raw_arguments(exchange):
    transfer = exchange.transfer
    handoff_id = str(uuid4())
    payload = {key: transfer[key] for key in (
        "source_subtask_id", "source_run_id", "source_agent_id", "target_agent_id",
    )}
    payload.update(handoff_id=handoff_id, completed_work_summary="请沿用这份歌词。",
                   constraints={"private": "never-send-constraints"})
    transfer.update(kind="ACCEPTED_HANDOFF", handoff_id=handoff_id, payload=payload,
                    payload_sha256=sha256(canonical_json_bytes(payload)).hexdigest())
    inputs = exchange.run.work_item_snapshot["work_item"]["input"]
    inputs["dependency_outputs"] = {}
    inputs["accepted_handoffs"] = [payload]
    notice, subject = exchange.project()
    assert subject is not None
    rendered = str(build_card(notice, subject, task_base_url=None, include_content=True))
    assert "已接受的交接" in rendered and "请沿用这份歌词" in rendered
    assert "never-send-constraints" not in rendered
