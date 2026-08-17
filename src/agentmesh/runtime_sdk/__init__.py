"""Framework-neutral Managed Agent Runtime v1 public contract.

This package intentionally contains only standard-library DTOs, validation, and
canonicalization.  It is safe to import from an adapter or an independently
packaged runtime; it must not acquire application, persistence, or framework
dependencies.
"""

from .canonical import (
    CANONICALIZATION_VERSION,
    CanonicalizationError,
    canonical_digest,
    canonical_json,
    canonical_json_bytes,
    decode_json,
    normalize_utc,
    sha256_digest,
)
from .models import (
    API_VERSION,
    RUNTIME_API_VERSION,
    ArtifactRef,
    DispatchReceipt,
    Envelope,
    ErrorCategory,
    LifecycleReceipt,
    RetryDisposition,
    RuntimeAssignment,
    RuntimeCapabilities,
    RuntimeContractError,
    RuntimeDescriptor,
    RuntimeError,
    RuntimeErrorDTO,
    RuntimeEvent,
    RuntimeEventPage,
    RuntimeExecutionHandle,
    RuntimeLimits,
    RuntimeObservation,
    RuntimePhase,
    RuntimeResult,
    UnknownCapability,
    UnknownMajorVersion,
    UnknownSecurityObligation,
    ValidationReport,
)
from .ports import ManagedAgentRuntime

__all__ = [
    "API_VERSION",
    "RUNTIME_API_VERSION",
    "CANONICALIZATION_VERSION",
    "CanonicalizationError",
    "ArtifactRef",
    "DispatchReceipt",
    "Envelope",
    "ErrorCategory",
    "LifecycleReceipt",
    "ManagedAgentRuntime",
    "RetryDisposition",
    "RuntimeAssignment",
    "RuntimeCapabilities",
    "RuntimeDescriptor",
    "RuntimeErrorDTO",
    "RuntimeError",
    "RuntimeEvent",
    "RuntimeEventPage",
    "RuntimeExecutionHandle",
    "RuntimeLimits",
    "RuntimeObservation",
    "RuntimePhase",
    "RuntimeResult",
    "RuntimeContractError",
    "UnknownCapability",
    "UnknownMajorVersion",
    "UnknownSecurityObligation",
    "canonical_digest",
    "canonical_json",
    "canonical_json_bytes",
    "decode_json",
    "normalize_utc",
    "sha256_digest",
    "ValidationReport",
]
