"""The small adapter port exposed by the Runtime SDK."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from .models import (
    DispatchReceipt,
    LifecycleReceipt,
    RuntimeAssignment,
    RuntimeDescriptor,
    RuntimeEventPage,
    RuntimeExecutionHandle,
    RuntimeObservation,
    ValidationReport,
)


class ManagedAgentRuntime(Protocol):
    """Framework-neutral provider boundary.

    Implementations own provider state only.  They cannot receive repositories,
    database connections, permits, or a control-plane container through this
    interface.  Lifecycle ``timeout`` values are transport budgets: adapters
    must enforce them at the provider boundary and must not reinterpret them as
    or mutate the durable business deadline.
    """

    def descriptor(self) -> RuntimeDescriptor: ...

    def validate(self, assignment: RuntimeAssignment) -> ValidationReport: ...

    def dispatch(self, assignment: RuntimeAssignment, *, dispatch_key: str) -> DispatchReceipt: ...

    def inspect(self, handle: RuntimeExecutionHandle) -> RuntimeObservation: ...

    def read_events(
        self, handle: RuntimeExecutionHandle, *, cursor: str | None, limit: int
    ) -> RuntimeEventPage: ...

    def request_cancel(
        self,
        handle: RuntimeExecutionHandle,
        *,
        cancellation_id: str,
        deadline: datetime,
        timeout: timedelta | None = None,
    ) -> LifecycleReceipt: ...

    def request_pause(
        self,
        handle: RuntimeExecutionHandle,
        *,
        operation_id: str,
        timeout: timedelta | None = None,
    ) -> LifecycleReceipt: ...

    def request_resume(
        self,
        handle: RuntimeExecutionHandle,
        *,
        operation_id: str,
        timeout: timedelta | None = None,
    ) -> LifecycleReceipt: ...

    def close(self) -> None: ...
