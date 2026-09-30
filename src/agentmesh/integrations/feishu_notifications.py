"""Durable, opt-in Feishu delivery. No external call is made in a Task transaction."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.infrastructure.postgres.models import (
    FeishuNotificationRecord,
    GovernedActionRecord,
    TaskRecord,
)

logger = logging.getLogger(__name__)
_API_ROOT = "https://open.feishu.cn/open-apis"
_EVENT_TITLES = {
    "COMPLETED": "AgentMesh · 任务完成",
    "FAILED": "AgentMesh · 任务失败",
    "WAITING_APPROVAL": "AgentMesh · 等待人工处理",
    "PENDING_APPROVAL": "AgentMesh · 等待治理审批",
}


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
    ) -> TaskRecord | GovernedActionRecord | None:
        with self._session_factory() as session:
            model = (
                TaskRecord if notification.subject_type == "TASK" else GovernedActionRecord
                if notification.subject_type == "GOVERNED_ACTION" else None
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
    subject: TaskRecord | GovernedActionRecord,
    *,
    task_base_url: str | None,
    include_content: bool,
) -> dict[str, object]:
    title = _EVENT_TITLES[notification.event_kind]
    subject_label = "任务 ID" if notification.subject_type == "TASK" else "审批请求 ID"
    elements: list[dict[str, object]] = [
        {"tag": "div", "text": {"tag": "plain_text", "content": f"{subject_label}：{subject.id}"}}
    ]
    if include_content and isinstance(subject, TaskRecord):
        elements.append(
            {"tag": "div", "text": {"tag": "plain_text", "content": subject.objective[:300]}}
        )
        if (
            notification.event_kind == "COMPLETED"
            and isinstance(subject.output, dict)
            and "agentmesh_deliverable_acceptance" not in (subject.input or {})
        ):
            summary = subject.output.get("summary")
            if isinstance(summary, str) and summary.strip():
                elements.append(
                    {"tag": "div", "text": {"tag": "plain_text", "content": summary[:500]}}
                )
        elif (
            notification.event_kind == "COMPLETED"
            and "agentmesh_deliverable_acceptance" in (subject.input or {})
        ):
            elements.append({"tag": "div", "text": {"tag": "plain_text", "content":
                "执行已完成；交付物已启用独立验收，请在 AgentMesh 查看验收状态。"}})
        elif notification.event_kind == "FAILED" and subject.error:
            elements.append(
                {"tag": "div", "text": {"tag": "plain_text", "content": subject.error[:300]}}
            )
    elif include_content and isinstance(subject, GovernedActionRecord):
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "plain_text",
                    "content": f"{subject.action_type} · {subject.resource_type}",
                },
            }
        )
    if task_base_url:
        query = "task" if notification.subject_type == "TASK" else "approval"
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "在 AgentMesh 查看"},
                        "type": "primary",
                        "url": f"{task_base_url.rstrip('/')}/?{query}={subject.id}",
                    }
                ],
            }
        )
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
    ) -> None:
        self._worker_id = worker_id
        self._store = store
        self._client = client
        self._task_base_url = task_base_url
        self._include_content = include_content

    def run_once(self) -> int:
        claimed = self._store.claim(worker_id=self._worker_id)
        for notification in claimed:
            try:
                subject = self._store.subject(notification)
                stale_task = (
                    notification.subject_type == "TASK"
                    and (subject is None or subject.status != notification.event_kind)
                )
                stale_approval = (
                    notification.subject_type == "GOVERNED_ACTION"
                    and (
                        subject is None
                        or subject.approval_status != "PENDING"
                        or subject.expires_at <= datetime.now(timezone.utc)
                    )
                )
                if stale_task or stale_approval or subject is None:
                    self._store.finish(notification, worker_id=self._worker_id, status="SKIPPED")
                    continue
                self._client.send(
                    notification_id=notification.id,
                    card=build_card(
                        notification,
                        subject,
                        task_base_url=self._task_base_url,
                        include_content=self._include_content,
                    ),
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
                    error=f"{type(exc).__name__}: {str(exc)[:180]}",
                )
        return len(claimed)
