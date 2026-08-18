"""Deterministic, side-by-side Runtime comparison without changing authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from agentmesh.runtime_sdk import canonical_digest


@dataclass(frozen=True)
class RuntimeComparisonSnapshot:
    terminal_state: str
    output: Any
    usage: dict[str, Any]
    artifact_refs: tuple[str, ...] = ()
    review: dict[str, Any] | None = None
    revision: int = 0
    audit: dict[str, Any] | None = None
    evidence_id: str | None = None

    def digest(self) -> str:
        return canonical_digest(
            {
                "terminal_state": self.terminal_state,
                "output": self.output,
                "usage": self.usage,
                "artifact_refs": list(self.artifact_refs),
                "review": self.review,
                "revision": self.revision,
                "audit": self.audit,
            }
        )


@dataclass(frozen=True)
class RuntimeComparisonReport:
    task_id: UUID
    run_id: UUID
    authoritative_path: str
    authoritative_digest: str
    comparison_digest: str
    mismatches: tuple[str, ...]

    @property
    def matches(self) -> bool:
        return not self.mismatches

    def identity_digest(self) -> str:
        """Stable identity for the complete immutable comparison report."""
        return canonical_digest(
            {
                "task_id": str(self.task_id),
                "run_id": str(self.run_id),
                "authoritative_path": self.authoritative_path,
                "authoritative_digest": self.authoritative_digest,
                "comparison_digest": self.comparison_digest,
                "mismatches": list(self.mismatches),
            }
        )


@dataclass(frozen=True)
class RuntimeComparisonRecord:
    id: UUID
    tenant_id: str
    task_id: UUID
    run_id: UUID
    attempt_id: UUID
    report: RuntimeComparisonReport
    created_at: datetime
    comparison_observation_id: str | None = None


def compare_snapshots(
    *,
    task_id: UUID,
    run_id: UUID,
    authoritative: RuntimeComparisonSnapshot,
    comparison: RuntimeComparisonSnapshot,
    authoritative_path: str,
) -> RuntimeComparisonReport:
    mismatches = tuple(
        name
        for name, left, right in (
            ("terminal_state", authoritative.terminal_state, comparison.terminal_state),
            (
                "output_digest",
                canonical_digest(authoritative.output),
                canonical_digest(comparison.output),
            ),
            ("usage", authoritative.usage, comparison.usage),
            ("artifact_refs", authoritative.artifact_refs, comparison.artifact_refs),
            ("review", authoritative.review, comparison.review),
            ("revision", authoritative.revision, comparison.revision),
            ("audit", authoritative.audit, comparison.audit),
        )
        if left != right
    )
    return RuntimeComparisonReport(
        task_id=task_id,
        run_id=run_id,
        authoritative_path=authoritative_path,
        authoritative_digest=authoritative.digest(),
        comparison_digest=comparison.digest(),
        mismatches=mismatches,
    )
