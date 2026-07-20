from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from agentmesh.domain.errors import InvalidA2ADelegation, InvalidA2ADelegationTransition
from agentmesh.domain.tasks import utc_now


class RemoteCorrelationStatus(str, Enum):
    PREPARED = "PREPARED"
    SENDING = "SENDING"
    WAITING_REMOTE = "WAITING_REMOTE"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    INTERVENTION_REQUIRED = "INTERVENTION_REQUIRED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"


TERMINAL_CORRELATION_STATUSES = {
    RemoteCorrelationStatus.COMPLETED,
    RemoteCorrelationStatus.FAILED,
    RemoteCorrelationStatus.REJECTED,
    RemoteCorrelationStatus.CANCELED,
}


@dataclass(frozen=True)
class RemoteTaskCorrelation:
    id: UUID
    tenant_id: str
    task_id: UUID
    run_id: UUID
    peer_id: UUID
    card_snapshot_id: UUID
    card_digest: str
    endpoint_url: str
    protocol_binding: str
    protocol_version: str
    endpoint_tenant: str | None
    outbound_message_id: UUID
    request_digest: str
    status: RemoteCorrelationStatus
    remote_task_id: str | None
    remote_context_id: str | None
    last_remote_state: str | None
    last_response_digest: str | None
    result: dict[str, Any] | None
    error: str | None
    poll_count: int
    late_result: bool
    created_at: datetime
    updated_at: datetime
    send_started_at: datetime | None
    terminal_at: datetime | None
    revision: int = 1

    @classmethod
    def prepare(
        cls,
        *,
        tenant_id: str,
        task_id: UUID,
        run_id: UUID,
        peer_id: UUID,
        card_snapshot_id: UUID,
        card_digest: str,
        endpoint_url: str,
        protocol_binding: str,
        protocol_version: str,
        endpoint_tenant: str | None,
        outbound_message_id: UUID,
        request_digest: str,
    ) -> RemoteTaskCorrelation:
        if not tenant_id.strip() or not card_digest.startswith("sha256:"):
            raise InvalidA2ADelegation("Remote correlation tenant and Card digest are required")
        if not request_digest.startswith("sha256:"):
            raise InvalidA2ADelegation("Remote correlation request digest is invalid")
        if protocol_binding != "HTTP+JSON" or protocol_version != "1.0":
            raise InvalidA2ADelegation("Only the A2A 1.0 HTTP+JSON binding is supported")
        now = utc_now()
        return cls(
            id=uuid4(),
            tenant_id=tenant_id.strip(),
            task_id=task_id,
            run_id=run_id,
            peer_id=peer_id,
            card_snapshot_id=card_snapshot_id,
            card_digest=card_digest,
            endpoint_url=endpoint_url,
            protocol_binding=protocol_binding,
            protocol_version=protocol_version,
            endpoint_tenant=endpoint_tenant,
            outbound_message_id=outbound_message_id,
            request_digest=request_digest,
            status=RemoteCorrelationStatus.PREPARED,
            remote_task_id=None,
            remote_context_id=None,
            last_remote_state=None,
            last_response_digest=None,
            result=None,
            error=None,
            poll_count=0,
            late_result=False,
            created_at=now,
            updated_at=now,
            send_started_at=None,
            terminal_at=None,
        )

    def mark_sending(self) -> RemoteTaskCorrelation:
        self._require(RemoteCorrelationStatus.PREPARED, "start send")
        now = utc_now()
        return replace(
            self,
            status=RemoteCorrelationStatus.SENDING,
            send_started_at=now,
            updated_at=now,
            revision=self.revision + 1,
        )

    def wait_remote(
        self,
        *,
        remote_task_id: str,
        remote_context_id: str | None,
        remote_state: str,
        response_digest: str,
        from_poll: bool,
    ) -> RemoteTaskCorrelation:
        self._require_update_source(from_poll)
        normalized_task_id = remote_task_id.strip()
        if not normalized_task_id:
            raise InvalidA2ADelegation("Remote Task ID must not be blank")
        self._assert_remote_identity(normalized_task_id)
        return replace(
            self,
            status=RemoteCorrelationStatus.WAITING_REMOTE,
            remote_task_id=normalized_task_id,
            remote_context_id=(remote_context_id.strip() if remote_context_id else None),
            last_remote_state=remote_state,
            last_response_digest=response_digest,
            error=None,
            poll_count=self.poll_count + (1 if from_poll else 0),
            updated_at=utc_now(),
            revision=self.revision + 1,
        )

    def outcome_unknown(self, *, error: str) -> RemoteTaskCorrelation:
        self._require(RemoteCorrelationStatus.SENDING, "mark outcome unknown")
        return replace(
            self,
            status=RemoteCorrelationStatus.OUTCOME_UNKNOWN,
            error=_required_error(error),
            updated_at=utc_now(),
            revision=self.revision + 1,
        )

    def intervention(
        self,
        *,
        remote_task_id: str | None,
        remote_context_id: str | None,
        remote_state: str,
        response_digest: str,
        error: str,
        from_poll: bool,
    ) -> RemoteTaskCorrelation:
        self._require_update_source(from_poll)
        if remote_task_id:
            self._assert_remote_identity(remote_task_id)
        return replace(
            self,
            status=RemoteCorrelationStatus.INTERVENTION_REQUIRED,
            remote_task_id=remote_task_id or self.remote_task_id,
            remote_context_id=remote_context_id or self.remote_context_id,
            last_remote_state=remote_state,
            last_response_digest=response_digest,
            error=_required_error(error),
            poll_count=self.poll_count + (1 if from_poll else 0),
            updated_at=utc_now(),
            revision=self.revision + 1,
        )

    def terminal(
        self,
        *,
        status: RemoteCorrelationStatus,
        remote_task_id: str | None,
        remote_context_id: str | None,
        remote_state: str,
        response_digest: str,
        result: dict[str, Any] | None,
        error: str | None,
        late_result: bool,
        from_poll: bool,
    ) -> RemoteTaskCorrelation:
        if status not in TERMINAL_CORRELATION_STATUSES:
            raise InvalidA2ADelegation("Remote terminal status is invalid")
        self._require_update_source(from_poll)
        if remote_task_id:
            self._assert_remote_identity(remote_task_id)
        now = utc_now()
        return replace(
            self,
            status=status,
            remote_task_id=remote_task_id or self.remote_task_id,
            remote_context_id=remote_context_id or self.remote_context_id,
            last_remote_state=remote_state,
            last_response_digest=response_digest,
            result=dict(result) if result is not None else None,
            error=_required_error(error) if error is not None else None,
            poll_count=self.poll_count + (1 if from_poll else 0),
            late_result=late_result,
            updated_at=now,
            terminal_at=now,
            revision=self.revision + 1,
        )

    def fail_before_send(self, *, error: str) -> RemoteTaskCorrelation:
        self._require(RemoteCorrelationStatus.SENDING, "fail before send")
        now = utc_now()
        return replace(
            self,
            status=RemoteCorrelationStatus.FAILED,
            error=_required_error(error),
            updated_at=now,
            terminal_at=now,
            revision=self.revision + 1,
        )

    def _require_update_source(self, from_poll: bool) -> None:
        expected = (
            {RemoteCorrelationStatus.WAITING_REMOTE, RemoteCorrelationStatus.INTERVENTION_REQUIRED}
            if from_poll
            else {RemoteCorrelationStatus.SENDING}
        )
        if self.status not in expected:
            raise InvalidA2ADelegationTransition(
                f"Cannot apply remote update from {self.status.value}"
            )

    def _assert_remote_identity(self, remote_task_id: str) -> None:
        if self.remote_task_id is not None and self.remote_task_id != remote_task_id:
            raise InvalidA2ADelegation("Remote Task identity changed for an existing correlation")

    def _require(self, expected: RemoteCorrelationStatus, action: str) -> None:
        if self.status is not expected:
            raise InvalidA2ADelegationTransition(
                f"Cannot {action} correlation {self.id} from {self.status.value}"
            )


def _required_error(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise InvalidA2ADelegation("Remote correlation error must not be blank")
    return normalized[:2000]
