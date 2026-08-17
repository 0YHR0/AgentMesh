"""The small adapter port exposed by the Runtime SDK."""

from __future__ import annotations

from datetime import datetime
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
    interface.
    """

    def descriptor(self) -> RuntimeDescriptor: ...

    def validate(self, assignment: RuntimeAssignment) -> ValidationReport: ...

    def dispatch(self, assignment: RuntimeAssignment, *, dispatch_key: str) -> DispatchReceipt: ...

    def inspect(self, handle: RuntimeExecutionHandle) -> RuntimeObservation: ...

    def read_events(
        self, handle: RuntimeExecutionHandle, *, cursor: str | None, limit: int
    ) -> RuntimeEventPage: ...

    def request_cancel(
        self, handle: RuntimeExecutionHandle, *, cancellation_id: str, deadline: datetime
    ) -> LifecycleReceipt: ...

    def request_pause(
        self, handle: RuntimeExecutionHandle, *, operation_id: str
    ) -> LifecycleReceipt: ...

    def request_resume(
        self, handle: RuntimeExecutionHandle, *, operation_id: str
    ) -> LifecycleReceipt: ...

    def close(self) -> None: ...
