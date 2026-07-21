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
    CANCELING = "CANCELING"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCEL_OUTCOME_UNKNOWN = "CANCEL_OUTCOME_UNKNOWN"
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
    credential_binding_id: UUID | None
    credential_scheme_name: str | None
    credential_scopes: tuple[str, ...]
    last_credential_lease_id: UUID | None
    status: RemoteCorrelationStatus
    remote_task_id: str | None
    remote_context_id: str | None
    last_remote_state: str | None
    last_response_digest: str | None
    result: dict[str, Any] | None
    error: str | None
    poll_count: int
    poll_failure_count: int
    next_poll_at: datetime | None
    last_polled_at: datetime | None
    poll_lease_owner: str | None
    poll_lease_expires_at: datetime | None
    cancel_requested_at: datetime | None
    cancel_request_count: int
    cancel_request_digest: str | None
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
        credential_binding_id: UUID | None = None,
        credential_scheme_name: str | None = None,
        credential_scopes: tuple[str, ...] = (),
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
            credential_binding_id=credential_binding_id,
            credential_scheme_name=credential_scheme_name,
            credential_scopes=tuple(credential_scopes),
            last_credential_lease_id=None,
            status=RemoteCorrelationStatus.PREPARED,
            remote_task_id=None,
            remote_context_id=None,
            last_remote_state=None,
            last_response_digest=None,
            result=None,
            error=None,
            poll_count=0,
            poll_failure_count=0,
            next_poll_at=None,
            last_polled_at=None,
            poll_lease_owner=None,
            poll_lease_expires_at=None,
            cancel_requested_at=None,
            cancel_request_count=0,
            cancel_request_digest=None,
            late_result=False,
            created_at=now,
            updated_at=now,
            send_started_at=None,
            terminal_at=None,
        )

    def attach_credential_lease(self, lease_id: UUID) -> RemoteTaskCorrelation:
        if self.credential_binding_id is None:
            raise InvalidA2ADelegation("Unauthenticated correlation cannot use a CredentialLease")
        if self.status not in {
            RemoteCorrelationStatus.SENDING,
            RemoteCorrelationStatus.WAITING_REMOTE,
            RemoteCorrelationStatus.INTERVENTION_REQUIRED,
            RemoteCorrelationStatus.CANCELING,
            RemoteCorrelationStatus.CANCEL_PENDING,
            RemoteCorrelationStatus.CANCEL_OUTCOME_UNKNOWN,
        }:
            raise InvalidA2ADelegationTransition(
                f"Cannot attach CredentialLease from {self.status.value}"
            )
        return replace(
            self,
            last_credential_lease_id=lease_id,
            updated_at=utc_now(),
            revision=self.revision + 1,
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
        next_poll_at: datetime,
        from_cancel: bool = False,
    ) -> RemoteTaskCorrelation:
        self._require_update_source(from_poll, from_cancel)
        normalized_task_id = remote_task_id.strip()
        if not normalized_task_id:
            raise InvalidA2ADelegation("Remote Task ID must not be blank")
        self._assert_remote_identity(normalized_task_id)
        now = utc_now()
        return replace(
            self,
            status=(
                RemoteCorrelationStatus.CANCEL_PENDING
                if from_cancel
                or self.status
                in {
                    RemoteCorrelationStatus.CANCELING,
                    RemoteCorrelationStatus.CANCEL_PENDING,
                    RemoteCorrelationStatus.CANCEL_OUTCOME_UNKNOWN,
                }
                else RemoteCorrelationStatus.WAITING_REMOTE
            ),
            remote_task_id=normalized_task_id,
            remote_context_id=(remote_context_id.strip() if remote_context_id else None),
            last_remote_state=remote_state,
            last_response_digest=response_digest,
            error=None,
            poll_count=self.poll_count + (1 if from_poll else 0),
            poll_failure_count=0,
            next_poll_at=next_poll_at,
            last_polled_at=now if from_poll else self.last_polled_at,
            poll_lease_owner=None,
            poll_lease_expires_at=None,
            updated_at=now,
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
        from_cancel: bool = False,
    ) -> RemoteTaskCorrelation:
        self._require_update_source(from_poll, from_cancel)
        if remote_task_id:
            self._assert_remote_identity(remote_task_id)
        now = utc_now()
        cancellation_update = from_cancel or (
            from_poll
            and self.status
            in {
                RemoteCorrelationStatus.CANCELING,
                RemoteCorrelationStatus.CANCEL_PENDING,
                RemoteCorrelationStatus.CANCEL_OUTCOME_UNKNOWN,
            }
        )
        return replace(
            self,
            status=(
                RemoteCorrelationStatus.CANCEL_PENDING
                if cancellation_update
                else RemoteCorrelationStatus.INTERVENTION_REQUIRED
            ),
            remote_task_id=remote_task_id or self.remote_task_id,
            remote_context_id=remote_context_id or self.remote_context_id,
            last_remote_state=remote_state,
            last_response_digest=response_digest,
            error=_required_error(error),
            poll_count=self.poll_count + (1 if from_poll else 0),
            poll_failure_count=0,
            next_poll_at=None,
            last_polled_at=now if from_poll else self.last_polled_at,
            poll_lease_owner=None,
            poll_lease_expires_at=None,
            updated_at=now,
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
        from_cancel: bool = False,
    ) -> RemoteTaskCorrelation:
        if status not in TERMINAL_CORRELATION_STATUSES:
            raise InvalidA2ADelegation("Remote terminal status is invalid")
        self._require_update_source(from_poll, from_cancel)
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
            poll_failure_count=0,
            next_poll_at=None,
            last_polled_at=now if from_poll else self.last_polled_at,
            poll_lease_owner=None,
            poll_lease_expires_at=None,
            late_result=late_result,
            updated_at=now,
            terminal_at=now,
            revision=self.revision + 1,
        )

    def claim_poll(
        self,
        *,
        owner: str,
        lease_expires_at: datetime,
        now: datetime | None = None,
        require_due: bool = True,
    ) -> RemoteTaskCorrelation:
        observed_at = now or utc_now()
        normalized_owner = owner.strip()
        if not normalized_owner or len(normalized_owner) > 128:
            raise InvalidA2ADelegation("A2A poll lease owner is invalid")
        if (
            self.status
            not in {
                RemoteCorrelationStatus.WAITING_REMOTE,
                RemoteCorrelationStatus.INTERVENTION_REQUIRED,
                RemoteCorrelationStatus.CANCELING,
                RemoteCorrelationStatus.CANCEL_PENDING,
                RemoteCorrelationStatus.CANCEL_OUTCOME_UNKNOWN,
            }
            or self.remote_task_id is None
        ):
            raise InvalidA2ADelegationTransition(f"Cannot claim poll from {self.status.value}")
        if lease_expires_at <= observed_at:
            raise InvalidA2ADelegation("A2A poll lease must expire in the future")
        if (
            self.poll_lease_owner is not None
            and self.poll_lease_expires_at is not None
            and self.poll_lease_expires_at > observed_at
        ):
            raise InvalidA2ADelegationTransition("A2A correlation already has an active poll lease")
        if require_due and (
            self.status
            not in {
                RemoteCorrelationStatus.WAITING_REMOTE,
                RemoteCorrelationStatus.CANCELING,
                RemoteCorrelationStatus.CANCEL_PENDING,
                RemoteCorrelationStatus.CANCEL_OUTCOME_UNKNOWN,
            }
            or self.next_poll_at is None
            or self.next_poll_at > observed_at
        ):
            raise InvalidA2ADelegationTransition("A2A correlation is not due for polling")
        return replace(
            self,
            poll_lease_owner=normalized_owner,
            poll_lease_expires_at=lease_expires_at,
            updated_at=observed_at,
            revision=self.revision + 1,
        )

    def poll_failed(
        self,
        *,
        owner: str,
        error: str,
        next_poll_at: datetime,
        max_failures: int,
    ) -> RemoteTaskCorrelation:
        if self.status not in {
            RemoteCorrelationStatus.WAITING_REMOTE,
            RemoteCorrelationStatus.INTERVENTION_REQUIRED,
            RemoteCorrelationStatus.CANCELING,
            RemoteCorrelationStatus.CANCEL_PENDING,
            RemoteCorrelationStatus.CANCEL_OUTCOME_UNKNOWN,
        }:
            raise InvalidA2ADelegationTransition(
                f"Cannot record poll failure from {self.status.value}"
            )
        if self.poll_lease_owner != owner:
            raise InvalidA2ADelegationTransition("A2A poll lease is not owned by this worker")
        if max_failures < 1:
            raise InvalidA2ADelegation("A2A poll failure limit must be positive")
        now = utc_now()
        failures = self.poll_failure_count + 1
        exhausted = (
            self.status is RemoteCorrelationStatus.INTERVENTION_REQUIRED or failures >= max_failures
        )
        cancellation_statuses = {
            RemoteCorrelationStatus.CANCELING,
            RemoteCorrelationStatus.CANCEL_PENDING,
            RemoteCorrelationStatus.CANCEL_OUTCOME_UNKNOWN,
        }
        return replace(
            self,
            status=(
                self.status
                if self.status in cancellation_statuses
                else (
                    RemoteCorrelationStatus.INTERVENTION_REQUIRED
                    if exhausted
                    else self.status
                )
            ),
            error=(
                f"reconcile_exhausted:{_required_error(error)}"
                if exhausted
                else _required_error(error)
            ),
            poll_count=self.poll_count + 1,
            poll_failure_count=failures,
            next_poll_at=None if exhausted else next_poll_at,
            last_polled_at=now,
            poll_lease_owner=None,
            poll_lease_expires_at=None,
            updated_at=now,
            revision=self.revision + 1,
        )

    def request_cancel(
        self,
        *,
        owner: str,
        request_digest: str,
        lease_expires_at: datetime,
    ) -> RemoteTaskCorrelation:
        if self.status not in {
            RemoteCorrelationStatus.WAITING_REMOTE,
            RemoteCorrelationStatus.INTERVENTION_REQUIRED,
        } or self.remote_task_id is None:
            raise InvalidA2ADelegationTransition(
                f"Cannot request remote cancellation from {self.status.value}"
            )
        normalized_owner = owner.strip()
        if not normalized_owner or len(normalized_owner) > 128:
            raise InvalidA2ADelegation("A2A cancellation lease owner is invalid")
        if not request_digest.startswith("sha256:"):
            raise InvalidA2ADelegation("A2A cancellation request digest is invalid")
        now = utc_now()
        if lease_expires_at <= now:
            raise InvalidA2ADelegation("A2A cancellation lease must expire in the future")
        if (
            self.poll_lease_owner is not None
            and self.poll_lease_expires_at is not None
            and self.poll_lease_expires_at > now
        ):
            raise InvalidA2ADelegationTransition("A2A correlation already has an active lease")
        return replace(
            self,
            status=RemoteCorrelationStatus.CANCELING,
            error=None,
            next_poll_at=lease_expires_at,
            poll_lease_owner=normalized_owner,
            poll_lease_expires_at=lease_expires_at,
            cancel_requested_at=self.cancel_requested_at or now,
            cancel_request_count=self.cancel_request_count + 1,
            cancel_request_digest=request_digest,
            updated_at=now,
            revision=self.revision + 1,
        )

    def cancel_delivery_failed(
        self,
        *,
        owner: str,
        error: str,
        request_may_have_been_sent: bool,
        next_poll_at: datetime,
    ) -> RemoteTaskCorrelation:
        self._require(RemoteCorrelationStatus.CANCELING, "record cancellation failure")
        if self.poll_lease_owner != owner:
            raise InvalidA2ADelegationTransition("A2A cancellation lease is not owned")
        now = utc_now()
        return replace(
            self,
            status=(
                RemoteCorrelationStatus.CANCEL_OUTCOME_UNKNOWN
                if request_may_have_been_sent
                else RemoteCorrelationStatus.WAITING_REMOTE
            ),
            error=_required_error(error),
            next_poll_at=next_poll_at,
            poll_lease_owner=None,
            poll_lease_expires_at=None,
            updated_at=now,
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

    def _require_update_source(self, from_poll: bool, from_cancel: bool = False) -> None:
        if from_poll and from_cancel:
            raise InvalidA2ADelegation("Remote update cannot be both poll and cancellation")
        if from_cancel:
            expected = {RemoteCorrelationStatus.CANCELING}
        elif from_poll:
            expected = {
                RemoteCorrelationStatus.WAITING_REMOTE,
                RemoteCorrelationStatus.INTERVENTION_REQUIRED,
                RemoteCorrelationStatus.CANCELING,
                RemoteCorrelationStatus.CANCEL_PENDING,
                RemoteCorrelationStatus.CANCEL_OUTCOME_UNKNOWN,
            }
        else:
            expected = {RemoteCorrelationStatus.SENDING}
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
