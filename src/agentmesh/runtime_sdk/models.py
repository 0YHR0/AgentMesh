"""Backward-compatible Runtime DTO imports.

Implementations are split by cohesive concern; consumers may continue importing
from agentmesh.runtime_sdk.models.
"""

from .assignment import RuntimeAssignment, RuntimeExecutionHandle  # noqa: F401
from .common import *  # noqa: F403
from .descriptor import (  # noqa: F401
    ArtifactRef,
    RuntimeCapabilities,
    RuntimeDescriptor,
    RuntimeLimits,
)
from .envelope import Envelope  # noqa: F401
from .observation import RuntimeErrorDTO, RuntimeObservation  # noqa: F401
from .receipts import (  # noqa: F401
    DispatchReceipt,
    LifecycleReceipt,
    RuntimeEvent,
    RuntimeEventPage,
    ValidationReport,
)
from .result import RuntimeResult  # noqa: F401

RuntimeError = RuntimeErrorDTO
