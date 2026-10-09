"""Durable, opt-in Feishu delivery. No external call is made in a Task transaction."""

from __future__ import annotations

import json
import logging
import math
import re
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit
from uuid import UUID

import httpx
from pydantic import ValidationError
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.domain.business_activity import ACTIVITY_SCHEMA, BusinessActivityNotice
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.infrastructure.postgres.models import (
    BusinessObjectRecord,
    BusinessObjectRevisionRecord,
    CompanyRecord,
    FeishuNotificationRecord,
    GovernedActionRecord,
    OutboxEventRecord,
    SubtaskRecord,
    TaskRecord,
    TaskRunRecord,
)
from agentmesh.runtime_sdk.canonical import canonical_json_bytes

logger = logging.getLogger(__name__)
_API_ROOT = "https://open.feishu.cn/open-apis"
_EVENT_TITLES = {
    "COMPLETED": "AgentMesh · 任务完成",
    "FAILED": "AgentMesh · 任务失败",
    "WAITING_APPROVAL": "AgentMesh · 等待人工处理",
    "PENDING_APPROVAL": "AgentMesh · 等待治理审批",
    "COLLAB_STARTED": "AgentMesh · 员工开始工作",
    "COLLAB_RESULT": "AgentMesh · 阶段成果",
    "COLLAB_FAILED": "AgentMesh · 执行失败",
    "ACTIVITY_STARTED": "AgentMesh · 员工开始工作",
    "ACTIVITY_RESULT": "AgentMesh · 阶段成果",
    "ACTIVITY_FAILED": "AgentMesh · 阶段未完成",
}
_COLLAB_EVENTS = frozenset({"COLLAB_STARTED", "COLLAB_RESULT", "COLLAB_FAILED"})
_MAX_TRANSFERS = 20
_SECRET = re.compile(
    r"(?i)(?:\b(?:sk-|mpg-|AQ\.)[\w.~-]{4,}|\bbearer\s+\S+|"
    r"\b(?:api[ _-]?key|access[ _-]?token|refresh[ _-]?token|authorization|"
    r"password|client[ _-]?secret|app[ _-]?secret)\s*[:=]\s*\S+)"
)
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_TOKEN_QUERY = re.compile(
    r"(?i)(?:token|secret|password|credential|signature|api.?key|authorization|"
    r"access.?key|^auth$|^key$|^sig$|^code$)"
)


def safe_content(value: object, *, limit: int = 500) -> str | None:
    """Allow bounded prose, never truncate before inspecting its entire secret surface.

    This prevents known credential formats, not semantic disclosure of private
    facts in otherwise innocuous prose. Raw inputs and memory are never selected.
    """
    if not isinstance(value, str) or len(value) > limit:
        return None
    text = unicodedata.normalize("NFKC", value)
    text = "".join(char for char in text if unicodedata.category(char) not in {"Cc", "Cf"})
    if any(0xD800 <= ord(char) <= 0xDFFF for char in text) or not text.strip():
        return None
    inspected = unquote(unquote(text))
    if _SECRET.search(inspected):
        return None
    for match in _URL.finditer(inspected):
        try:
            url = urlsplit(match.group())
            if url.username is not None or url.password is not None:
                return None
            if any(_TOKEN_QUERY.search(key) for key, _ in parse_qsl(url.query)):
                return None
        except ValueError:
            return None
    return text.strip() if len(text) <= limit else None


@dataclass(frozen=True)
class CollaborationTransfer:
    kind: str
    source_key: str
    source_agent_id: str | None
    target_key: str
    target_agent_id: str
    payload_sha256: str
    summary: str | None


@dataclass(frozen=True)
class CollaborationSubject:
    task_id: UUID
    run_id: UUID
    agent_id: str
    subtask_key: str
    role_label: str
    occurred_at: datetime
    status: str
    transfers: tuple[CollaborationTransfer, ...]
    output_summary: str | None
    discussion_topic: str | None = None
    discussion_turn: int | None = None
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class BusinessActivitySubject:
    object_id: UUID
    revision: int
    agent_id: str
    role_label: str
    phase: str
    occurred_at: datetime
    summary: str | None


def project_business_activity(notification, event, obj, revision, company):
    """Bind a notice to its immutable, same-tenant committed object revision."""
    try:
        envelope = MessageEnvelope.from_dict(event.envelope)
        payload = envelope.payload
        if (
            notification.subject_type != "BUSINESS_ACTIVITY"
            or event.id != notification.subject_id
            or event.tenant_id != notification.tenant_id
            or envelope.tenant_id != notification.tenant_id
            or envelope.message_id != event.id
            or event.topic != ACTIVITY_SCHEMA
            or envelope.schema_name != ACTIVITY_SCHEMA
            or envelope.schema_version != 1
            or set(payload) != {"company_id", "object_id", "revision", "data_digest", "activity"}
            or type(payload["revision"]) is not int
            or payload["revision"] < 1
            or company.tenant_id != notification.tenant_id
            or company.id != obj.company_id
            or str(company.id) != payload["company_id"]
            or str(obj.id) != payload["object_id"]
            or envelope.correlation_id != obj.id
            or revision.object_id != obj.id
            or revision.revision != payload["revision"]
            or revision.data_digest != payload["data_digest"]
        ):
            return None
        notice = BusinessActivityNotice.model_validate(payload["activity"])
        if notification.event_kind != f"ACTIVITY_{notice.phase}":
            return None
        agent_id = safe_content(notice.agent_id, limit=128)
        role_label = safe_content(notice.role_label, limit=80)
        if not agent_id or not role_label:
            return None
        return BusinessActivitySubject(
            obj.id,
            revision.revision,
            agent_id,
            role_label,
            notice.phase,
            envelope.occurred_at,
            safe_content(notice.summary) if notice.summary else None,
        )
    except (ValueError, TypeError, KeyError, AttributeError, ValidationError):
        return None


def project_collaboration_subject(
    notification: ClaimedNotification,
    *,
    task: TaskRecord,
    run: TaskRunRecord,
    subtask: SubtaskRecord,
    get_record: Callable[[Any, UUID], Any],
) -> CollaborationSubject | None:
    """Project an exact Run's pinned coordinator evidence, never latest Subtask output."""
    if (
        notification.subject_type != "TASK_RUN"
        or notification.event_kind not in _COLLAB_EVENTS
        or run.id != notification.subject_id
        or task.tenant_id != notification.tenant_id
        or task.id != run.task_id
        or task.execution_mode != "COORDINATED"
        or run.role != "EXECUTOR"
        or subtask.id != run.subtask_id
        or subtask.task_id != task.id
    ):
        return None
    snapshot = run.work_item_snapshot
    pinned_at = run.work_item_pinned_at
    if (
        not isinstance(snapshot, dict)
        or type(snapshot.get("schema_version")) is not int
        or snapshot["schema_version"] != 1
        or not isinstance(snapshot.get("work_item"), dict)
        or not isinstance(snapshot["work_item"].get("objective"), str)
        or not snapshot["work_item"]["objective"].strip()
        or not isinstance(snapshot["work_item"].get("input"), dict)
        or not isinstance(snapshot.get("transfers"), list)
        or not isinstance(pinned_at, datetime)
        or pinned_at.tzinfo is None
    ):
        return None
    try:
        if len(canonical_json_bytes(snapshot)) > 262_144:
            return None
    except (TypeError, ValueError, RecursionError):
        return None
    if notification.event_kind == "COLLAB_STARTED":
        occurred_at, status = pinned_at, "STARTED"
    elif notification.event_kind == "COLLAB_RESULT" and run.status == "SUCCEEDED":
        occurred_at, status = run.completed_at, "SUCCEEDED"
    elif notification.event_kind == "COLLAB_FAILED" and run.status in {"FAILED", "CANCELED"}:
        occurred_at, status = run.completed_at, run.status
    else:
        return None
    if not isinstance(occurred_at, datetime) or occurred_at.tzinfo is None:
        return None
    agent_id = safe_content(run.agent_id, limit=128)
    subtask_key = safe_content(subtask.key, limit=128)
    if agent_id is None or subtask_key is None:
        return None
    input_value = snapshot["work_item"]["input"]
    dependency_outputs = input_value.get("dependency_outputs", {})
    accepted_handoffs = input_value.get("accepted_handoffs", [])
    if not isinstance(dependency_outputs, dict) or not isinstance(accepted_handoffs, list):
        return None
    role_input = input_value.get("subtask_input")
    role_label = (
        safe_content(role_input.get("role"), limit=80) if isinstance(role_input, dict) else None
    )
    transfers: list[CollaborationTransfer] = []
    # Validate the complete evidence list even when only a bounded prefix is shown.
    if len(snapshot["transfers"]) > 1000:
        return None
    for raw in snapshot["transfers"]:
        if not isinstance(raw, dict) or raw.get("kind") not in {
            "DEPENDENCY_RESULT",
            "ACCEPTED_HANDOFF",
        }:
            return None
        try:
            source_id = UUID(raw["source_subtask_id"])
            source_run_id = UUID(raw["source_run_id"]) if raw.get("source_run_id") else None
            source = get_record(SubtaskRecord, source_id)
            source_run = get_record(TaskRunRecord, source_run_id) if source_run_id else None
            if (
                source is None
                or source.task_id != task.id
                or source.key != raw["source_key"]
                or raw["target_subtask_id"] != str(subtask.id)
                or raw["target_key"] != subtask.key
                or raw["target_run_id"] != str(run.id)
                or raw["target_agent_id"] != run.agent_id
                or (
                    source_run_id is not None
                    and (
                        source_run is None
                        or source_run.task_id != task.id
                        or source_run.subtask_id != source_id
                        or source_run.agent_id != raw.get("source_agent_id")
                    )
                )
            ):
                return None
            payload = raw["payload"]
            digest = sha256(canonical_json_bytes(payload)).hexdigest()
            if digest != raw["payload_sha256"]:
                return None
            if raw["kind"] == "DEPENDENCY_RESULT" and (
                raw["source_key"] not in dependency_outputs
                or dependency_outputs[raw["source_key"]] != payload
            ):
                return None
            if raw["kind"] == "ACCEPTED_HANDOFF":
                UUID(raw["handoff_id"])
                if source_run_id is None or payload not in accepted_handoffs:
                    return None
                if not isinstance(payload, dict) or any(
                    payload.get(key) != raw.get(key)
                    for key in (
                        "handoff_id",
                        "source_subtask_id",
                        "source_run_id",
                        "source_agent_id",
                        "target_agent_id",
                    )
                ):
                    return None
            source_key = safe_content(raw["source_key"], limit=128)
            source_agent = safe_content(raw.get("source_agent_id"), limit=128)
            if source_key is None or (
                raw.get("source_agent_id") is not None and source_agent is None
            ):
                return None
        except (TypeError, ValueError, KeyError, RecursionError):
            return None
        if len(transfers) < _MAX_TRANSFERS:
            summary_field = (
                "summary" if raw["kind"] == "DEPENDENCY_RESULT" else ("completed_work_summary")
            )
            summary = (
                safe_content(payload.get(summary_field)) if isinstance(payload, dict) else None
            )
            transfers.append(
                CollaborationTransfer(
                    kind=raw["kind"],
                    source_key=source_key,
                    source_agent_id=source_agent,
                    target_key=subtask_key,
                    target_agent_id=agent_id,
                    payload_sha256=digest,
                    summary=summary,
                )
            )
    output_summary = (
        safe_content(run.output.get("summary"))
        if (notification.event_kind == "COLLAB_RESULT" and isinstance(run.output, dict))
        else None
    )
    evidence_ids = ()
    if isinstance(role_input, dict) and role_input.get("collaboration_mode") == "discussion":
        evidence = role_input.get("audio_evidence", [])
        known = (
            {
                e["id"]
                for e in evidence
                if isinstance(e, dict)
                and isinstance(e.get("id"), str)
                and re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", e["id"])
            }
            if isinstance(evidence, list)
            else set()
        )
        explicit = run.output.get("evidence_ids") if isinstance(run.output, dict) else None
        if explicit is not None:
            if (
                not isinstance(explicit, list)
                or not 1 <= len(explicit) <= 14
                or any(not isinstance(ref, str) or ref not in known for ref in explicit)
            ):
                return None
            evidence_ids = tuple(dict.fromkeys(explicit))
        elif output_summary:
            evidence_ids = tuple(sorted(ref for ref in known if ref in output_summary))
    return CollaborationSubject(
        task_id=task.id,
        run_id=run.id,
        agent_id=agent_id,
        subtask_key=subtask_key,
        role_label=role_label or subtask_key,
        occurred_at=occurred_at.astimezone(timezone.utc),
        status=status,
        transfers=tuple(transfers),
        output_summary=output_summary,
        evidence_ids=evidence_ids,
        discussion_topic=(
            safe_content(role_input.get("discussion_topic"), limit=400)
            if isinstance(role_input, dict) and role_input.get("collaboration_mode") == "discussion"
            else None
        ),
        discussion_turn=(
            role_input["discussion_turn"]
            if isinstance(role_input, dict)
            and type(role_input.get("discussion_turn")) is int
            and 1 <= role_input["discussion_turn"] <= 20
            else None
        ),
    )


@dataclass(frozen=True)
class ClaimedNotification:
    id: UUID
    tenant_id: str
    subject_type: str
    subject_id: UUID
    event_kind: str
    attempt_count: int


class FeishuNotificationStore:
    def __init__(self, session_factory: sessionmaker[Session], *, tenant_id: str) -> None:
        self._session_factory = session_factory
        self._tenant_id = tenant_id

    def claim(self, *, worker_id: str, limit: int = 5) -> list[ClaimedNotification]:
        now = datetime.now(timezone.utc)
        with self._session_factory() as session, session.begin():
            rows = list(
                session.scalars(
                    select(FeishuNotificationRecord)
                    .where(
                        FeishuNotificationRecord.tenant_id == self._tenant_id,
                        FeishuNotificationRecord.status == "PENDING",
                        FeishuNotificationRecord.available_at <= now,
                        or_(
                            FeishuNotificationRecord.claimed_until.is_(None),
                            FeishuNotificationRecord.claimed_until <= now,
                        ),
                    )
                    .order_by(FeishuNotificationRecord.created_at, FeishuNotificationRecord.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            for row in rows:
                row.claimed_by = worker_id
                row.claimed_until = now + timedelta(seconds=90)
                row.attempt_count += 1
            return [
                ClaimedNotification(
                    id=row.id,
                    tenant_id=row.tenant_id,
                    subject_type=row.subject_type,
                    subject_id=row.subject_id,
                    event_kind=row.event_kind,
                    attempt_count=row.attempt_count,
                )
                for row in rows
            ]

    def subject(
        self, notification: ClaimedNotification
    ) -> TaskRecord | GovernedActionRecord | CollaborationSubject | BusinessActivitySubject | None:
        if notification.tenant_id != self._tenant_id:
            return None
        with self._session_factory() as session:
            if notification.subject_type == "BUSINESS_ACTIVITY":
                event = session.get(OutboxEventRecord, notification.subject_id)
                if event is None or event.tenant_id != self._tenant_id:
                    return None
                try:
                    payload = event.envelope["payload"]
                    obj = session.get(BusinessObjectRecord, UUID(payload["object_id"]))
                    if obj is None:
                        return None
                    company = session.get(CompanyRecord, obj.company_id)
                    revision = session.get(
                        BusinessObjectRevisionRecord, (obj.id, payload["revision"])
                    )
                    return project_business_activity(notification, event, obj, revision, company)
                except (ValueError, TypeError, KeyError):
                    return None
            if notification.subject_type == "TASK_RUN":
                run = session.scalar(
                    select(TaskRunRecord)
                    .join(TaskRecord, TaskRecord.id == TaskRunRecord.task_id)
                    .where(
                        TaskRunRecord.id == notification.subject_id,
                        TaskRecord.tenant_id == self._tenant_id,
                    )
                )
                if run is None or run.subtask_id is None:
                    return None
                task = session.get(TaskRecord, run.task_id)
                subtask = session.get(SubtaskRecord, run.subtask_id)
                if task is None or subtask is None:
                    return None
                return project_collaboration_subject(
                    notification,
                    task=task,
                    run=run,
                    subtask=subtask,
                    get_record=session.get,
                )
            model = (
                TaskRecord
                if notification.subject_type == "TASK"
                else GovernedActionRecord
                if notification.subject_type == "GOVERNED_ACTION"
                else None
            )
            if model is None:
                return None
            subject = session.get(model, notification.subject_id)
            if subject is None or subject.tenant_id != notification.tenant_id:
                return None
            session.expunge(subject)
            return subject

    def finish(self, notification: ClaimedNotification, *, worker_id: str, status: str) -> None:
        now = datetime.now(timezone.utc)
        with self._session_factory() as session, session.begin():
            session.execute(
                update(FeishuNotificationRecord)
                .where(
                    FeishuNotificationRecord.id == notification.id,
                    FeishuNotificationRecord.tenant_id == notification.tenant_id,
                    FeishuNotificationRecord.status == "PENDING",
                    FeishuNotificationRecord.claimed_by == worker_id,
                )
                .values(
                    status=status,
                    delivered_at=now if status == "DELIVERED" else None,
                    claimed_by=None,
                    claimed_until=None,
                    last_error=None,
                )
            )

    def fail(self, notification: ClaimedNotification, *, worker_id: str, error: str) -> None:
        now = datetime.now(timezone.utc)
        dead = notification.attempt_count >= 8
        delay = min(300, 5 * (2 ** (notification.attempt_count - 1)))
        with self._session_factory() as session, session.begin():
            session.execute(
                update(FeishuNotificationRecord)
                .where(
                    FeishuNotificationRecord.id == notification.id,
                    FeishuNotificationRecord.tenant_id == notification.tenant_id,
                    FeishuNotificationRecord.status == "PENDING",
                    FeishuNotificationRecord.claimed_by == worker_id,
                )
                .values(
                    status="DEAD" if dead else "PENDING",
                    available_at=now + timedelta(seconds=delay),
                    claimed_by=None,
                    claimed_until=None,
                    last_error=error[:255],
                )
            )


class FeishuClient:
    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        chat_id: str,
        timeout_seconds: int = 5,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._chat_id = chat_id
        self._http = http_client or httpx.Client(timeout=timeout_seconds, follow_redirects=False)
        self._owns_http = http_client is None
        self._token: str | None = None
        self._token_expires_at = 0.0

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def _post(self, path: str, *, body: dict[str, object], token: str | None = None) -> dict:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = self._http.post(f"{_API_ROOT}{path}", json=body, headers=headers)
        if response.status_code != 200:
            raise RuntimeError(f"Feishu HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("Feishu returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("code") != 0:
            code = payload.get("code") if isinstance(payload, dict) else "invalid"
            raise RuntimeError(f"Feishu API code {code}")
        return payload

    def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        payload = self._post(
            "/auth/v3/tenant_access_token/internal",
            body={"app_id": self._app_id, "app_secret": self._app_secret},
        )
        token = payload.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("Feishu token response missing tenant_access_token")
        self._token = token
        self._token_expires_at = time.monotonic() + max(0, int(payload.get("expire", 0)) - 60)
        return token

    def send(self, *, notification_id: UUID, card: dict[str, object]) -> None:
        try:
            self._post(
                "/im/v1/messages?receive_id_type=chat_id",
                token=self._access_token(),
                body={
                    "receive_id": self._chat_id,
                    "msg_type": "interactive",
                    "content": json.dumps(card, ensure_ascii=False, separators=(",", ":")),
                    "uuid": str(notification_id),
                },
            )
        except Exception:
            # A revoked/expired token must be refreshed on the next delivery attempt.
            self._token = None
            raise


def build_card(
    notification: ClaimedNotification,
    subject: TaskRecord | GovernedActionRecord | CollaborationSubject | BusinessActivitySubject,
    *,
    task_base_url: str | None,
    include_content: bool,
) -> dict[str, object]:
    title = _EVENT_TITLES[notification.event_kind]
    if isinstance(subject, BusinessActivitySubject):
        content = (
            f"业务对象 ID：{subject.object_id} · 版本 {subject.revision}"
            f"\n时间：{subject.occurred_at.isoformat()}"
            f"\n员工：{subject.agent_id} · {subject.role_label}"
            "\n这是明确记录的工作状态或业务摘要，不是模型内部推理或自由群聊。"
        )
        elements = []
        if include_content and subject.summary:
            elements.append(
                {"tag": "div", "text": {"tag": "plain_text", "content": subject.summary}}
            )
        elements.append(_details([_prose(content)]))
        return _card(subject.role_label, elements)
    if isinstance(subject, CollaborationSubject):
        if subject.discussion_topic and subject.discussion_turn:
            refs = ", ".join(subject.evidence_ids) if include_content else "内容同步关闭"
            if notification.event_kind == "COLLAB_RESULT":
                reply = subject.output_summary if include_content else None
                if reply:
                    for ref in sorted(subject.evidence_ids, key=len, reverse=True):
                        reply = re.sub(
                            rf"(?<![A-Za-z0-9_-]){re.escape(ref)}(?![A-Za-z0-9_-])", "", reply
                        )
                    reply = re.sub(r"[\[（(]\s*[\]）)]", "", reply).strip()
                content = reply or "这条创作意见未开启外部内容同步，请在私有工作台查看。"
            elif notification.event_kind == "COLLAB_STARTED":
                content = "正在阅读前面的意见，准备回应。"
            else:
                content = "这次回应未完成；没有编造发言，请在工作台检查。"
            elements = [{"tag": "div", "text": {"tag": "plain_text", "content": content}}]
            elements.append(
                _details(
                    [
                        _prose(
                            f"讨论 {subject.discussion_turn} · {subject.discussion_topic}\n"
                            f"Task {subject.task_id} · Run {subject.run_id}\n"
                            f"证据引用：{refs}\n"
                            "公开创作意见，不是内部推理或主人批准。"
                        )
                    ]
                )
            )
            _add_link(elements, task_base_url, query="task", subject_id=subject.task_id)
            return _card(subject.role_label, elements)
        elements: list[dict[str, object]] = []
        details: list[dict[str, object]] = []

        def text(content: str) -> None:
            elements.append({"tag": "div", "text": {"tag": "plain_text", "content": content}})

        details.append(_prose(f"任务 ID：{subject.task_id}\nRun ID：{subject.run_id}"))
        details.append(
            _prose(
                f"时间：{subject.occurred_at.isoformat()}\n员工：{subject.agent_id}"
                f"\n工作项：{subject.subtask_key} · {subject.role_label}"
            )
        )
        if notification.event_kind == "COLLAB_STARTED":
            text("我接着处理这部分，先看看前面的材料。")
            details.append(_prose("协调器已固定的输入交接，不是模型内部推理。"))
        elif notification.event_kind == "COLLAB_RESULT":
            details.append(_prose("本次执行的阶段成果，尚不代表最终交付或人工批准。"))
        else:
            text("本次 Run 执行失败；详细诊断请在 AgentMesh 内查看。")
        incoming = (
            subject.transfers[:_MAX_TRANSFERS]
            if (notification.event_kind == "COLLAB_STARTED")
            else ()
        )
        for transfer in incoming:
            label = "依赖结果" if transfer.kind == "DEPENDENCY_RESULT" else "已接受的交接"
            details.append(
                _prose(
                    f"{label}：{transfer.source_agent_id or '未记录员工'} / {transfer.source_key}"
                    f" → {transfer.target_agent_id} / {transfer.target_key}"
                    f"\n内容指纹：{transfer.payload_sha256}"
                )
            )
            if include_content and transfer.summary:
                text(transfer.summary)
        if include_content and notification.event_kind == "COLLAB_RESULT":
            text(
                subject.output_summary
                if subject.output_summary
                else ("本次 Run 未记录可安全同步的业务摘要。")
            )
        elements.append(_details(details))
        _add_link(elements, task_base_url, query="task", subject_id=subject.task_id)
        return _card(subject.role_label, elements)
    subject_label = "任务 ID" if notification.subject_type == "TASK" else "审批请求 ID"
    elements = []
    if include_content and isinstance(subject, TaskRecord):
        objective = safe_content(subject.objective, limit=300)
        if objective:
            elements.append({"tag": "div", "text": {"tag": "plain_text", "content": objective}})
        if (
            notification.event_kind == "COMPLETED"
            and isinstance(subject.output, dict)
            and "agentmesh_deliverable_acceptance" not in (subject.input or {})
        ):
            summary = safe_content(subject.output.get("summary"))
            if summary:
                elements.append({"tag": "div", "text": {"tag": "plain_text", "content": summary}})
        elif notification.event_kind == "COMPLETED" and "agentmesh_deliverable_acceptance" in (
            subject.input or {}
        ):
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "plain_text",
                        "content": (
                            "执行已完成；交付物已启用独立验收，请在 AgentMesh 查看验收状态。"
                        ),
                    },
                }
            )
        elif notification.event_kind == "FAILED" and subject.error:
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "plain_text",
                        "content": "执行失败；详细诊断请在 AgentMesh 内查看。",
                    },
                }
            )
    elif include_content and isinstance(subject, GovernedActionRecord):
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "plain_text",
                    "content": safe_content(
                        f"{subject.action_type} · {subject.resource_type}", limit=160
                    )
                    or "治理审批请求",
                },
            }
        )
    query = "task" if notification.subject_type == "TASK" else "approval"
    elements.append(_details([_prose(f"{subject_label}：{subject.id}")]))
    _add_link(elements, task_base_url, query=query, subject_id=subject.id)
    return _card(title, elements)


def _prose(content: str) -> dict[str, object]:
    return {"tag": "div", "text": {"tag": "plain_text", "content": content}}


def _details(elements: list[dict[str, object]]) -> dict[str, object]:
    """Feishu JSON 1.0 native panel; client-side expansion needs no callback."""
    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "header": {"title": {"tag": "plain_text", "content": "技术详情"}},
        "elements": elements,
    }


def _add_link(
    elements: list[dict[str, object]],
    task_base_url: str | None,
    *,
    query: str,
    subject_id: UUID,
) -> None:
    if task_base_url and safe_content(task_base_url, limit=2048):
        try:
            url = urlsplit(task_base_url)
            if url.scheme != "https" or not url.hostname or url.query or url.fragment:
                return
        except ValueError:
            return
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "在 AgentMesh 查看"},
                        "type": "primary",
                        "url": f"{task_base_url.rstrip('/')}/?{query}={subject_id}",
                    }
                ],
            }
        )


def _card(title: str, elements: list[dict[str, object]]) -> dict[str, object]:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
        "elements": elements,
    }


class FeishuNotificationWorker:
    def __init__(
        self,
        *,
        worker_id: str,
        store: FeishuNotificationStore,
        client: FeishuClient,
        task_base_url: str | None,
        include_content: bool,
        sync_collaboration: bool = False,
        discussion_results_only: bool = True,
        employee_clients: Mapping[str, FeishuClient] | None = None,
        employee_bot_fallback: bool = True,
        send_interval_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._worker_id = worker_id
        self._store = store
        self._client = client
        self._task_base_url = task_base_url
        self._include_content = include_content
        if not math.isfinite(send_interval_seconds) or not 0 <= send_interval_seconds <= 10:
            raise ValueError("Feishu send interval must be between 0 and 10 seconds")
        self._sync_collaboration = sync_collaboration
        self._discussion_results_only = discussion_results_only
        if employee_clients is not None and (not sync_collaboration or not employee_clients):
            raise ValueError(
                "Employee bot routing requires collaboration sync and nonempty bindings"
            )
        self._employee_clients = dict(employee_clients or {})
        self._employee_bot_fallback = employee_bot_fallback
        self._send_interval_seconds = send_interval_seconds
        self._sleep = sleep
        self._last_send_at: float | None = None

    def run_once(self) -> int:
        claimed = self._store.claim(worker_id=self._worker_id)
        for notification in claimed:
            try:
                if (
                    notification.subject_type in {"TASK_RUN", "BUSINESS_ACTIVITY"}
                    and not self._sync_collaboration
                ):
                    self._store.finish(notification, worker_id=self._worker_id, status="SKIPPED")
                    continue
                subject = self._store.subject(notification)
                stale_task = notification.subject_type == "TASK" and (
                    subject is None or subject.status != notification.event_kind
                )
                stale_approval = notification.subject_type == "GOVERNED_ACTION" and (
                    subject is None
                    or subject.approval_status != "PENDING"
                    or subject.expires_at <= datetime.now(timezone.utc)
                )
                invalid_collaboration = notification.subject_type == "TASK_RUN" and not isinstance(
                    subject, CollaborationSubject
                )
                invalid_activity = (
                    notification.subject_type == "BUSINESS_ACTIVITY"
                    and not isinstance(subject, BusinessActivitySubject)
                )
                if (
                    stale_task
                    or stale_approval
                    or invalid_collaboration
                    or invalid_activity
                    or subject is None
                ):
                    self._store.finish(notification, worker_id=self._worker_id, status="SKIPPED")
                    continue
                if (
                    self._discussion_results_only
                    and isinstance(subject, CollaborationSubject)
                    and subject.discussion_topic
                    and subject.discussion_turn
                    and notification.event_kind == "COLLAB_STARTED"
                ):
                    # Keep the durable Run/event; do not flood a discussion with typing notices.
                    self._store.finish(notification, worker_id=self._worker_id, status="SKIPPED")
                    continue
                card = build_card(
                    notification,
                    subject,
                    task_base_url=self._task_base_url,
                    include_content=self._include_content,
                )
                if self._last_send_at is not None:
                    elapsed = time.monotonic() - self._last_send_at
                    remaining = self._send_interval_seconds - elapsed
                    if remaining > 0:
                        self._sleep(remaining)
                self._last_send_at = time.monotonic()
                client = self._client
                if (
                    isinstance(subject, (CollaborationSubject, BusinessActivitySubject))
                    and self._employee_clients
                ):
                    employee_client = self._employee_clients.get(subject.agent_id)
                    if employee_client is not None:
                        client = employee_client
                    elif not self._employee_bot_fallback:
                        raise RuntimeError("Employee bot binding missing; delivery withheld")
                client.send(
                    notification_id=notification.id,
                    card=card,
                )
                self._store.finish(notification, worker_id=self._worker_id, status="DELIVERED")
            except Exception as exc:
                # Never log request/response bodies or credentials. The job is independent of Task.
                logger.warning(
                    "Feishu notification %s failed: %s", notification.id, type(exc).__name__
                )
                self._store.fail(
                    notification,
                    worker_id=self._worker_id,
                    error=f"{type(exc).__name__}: "
                    + (safe_content(str(exc), limit=180) or "delivery error details withheld"),
                )
        return len(claimed)
