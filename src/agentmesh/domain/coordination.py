from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from agentmesh.domain.errors import InvalidTaskInput, InvalidTaskTransition

AGENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,62}$")
CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+$")


def normalize_agent_name(value: str) -> str:
    normalized = value.strip().lower()
    if not AGENT_NAME_PATTERN.fullmatch(normalized):
        raise InvalidTaskInput(
            "Preferred Agent name must be 3-63 lowercase letters, numbers, or hyphens"
        )
    return normalized


def validate_capability_key(value: str) -> str:
    normalized = value.strip().lower()
    if not CAPABILITY_PATTERN.fullmatch(normalized):
        raise InvalidTaskInput(
            "Capability key must be a namespaced value such as code.review.python"
        )
    return normalized


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SubtaskStatus(str, Enum):
    BLOCKED = "BLOCKED"
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


TERMINAL_SUBTASK_STATUSES = {
    SubtaskStatus.COMPLETED,
    SubtaskStatus.FAILED,
    SubtaskStatus.CANCELED,
}


@dataclass(frozen=True)
class SubtaskSpec:
    key: str
    objective: str
    input: dict[str, Any]
    required_capabilities: tuple[str, ...]
    depends_on: tuple[str, ...]
    preferred_agent_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        key: str,
        objective: str,
        input: dict[str, Any] | None = None,
        required_capabilities: tuple[str, ...] | list[str] = ("general.task",),
        depends_on: tuple[str, ...] | list[str] = (),
        preferred_agent_id: str | None = None,
    ) -> SubtaskSpec:
        normalized_key = key.strip()
        normalized_objective = objective.strip()
        if not normalized_key or len(normalized_key) > 128:
            raise InvalidTaskInput("Subtask key must contain 1 to 128 characters")
        if not normalized_objective:
            raise InvalidTaskInput("Subtask objective must not be empty")
        capabilities = tuple(
            sorted({validate_capability_key(value) for value in required_capabilities})
        )
        if not capabilities:
            raise InvalidTaskInput("Subtask requires at least one capability")
        dependencies = tuple(dict.fromkeys(value.strip() for value in depends_on))
        if any(not value for value in dependencies):
            raise InvalidTaskInput("Subtask dependencies must not be empty")
        agent_id = (
            normalize_agent_name(preferred_agent_id)
            if preferred_agent_id is not None
            else None
        )
        return cls(
            key=normalized_key,
            objective=normalized_objective,
            input=dict(input or {}),
            required_capabilities=capabilities,
            depends_on=dependencies,
            preferred_agent_id=agent_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "objective": self.objective,
            "input": dict(self.input),
            "required_capabilities": list(self.required_capabilities),
            "depends_on": list(self.depends_on),
            "preferred_agent_id": self.preferred_agent_id,
        }


@dataclass(frozen=True)
class CoordinatedPlan:
    version: int
    digest: str
    max_concurrency: int
    specs: tuple[SubtaskSpec, ...]

    @classmethod
    def create(
        cls,
        specs: tuple[SubtaskSpec, ...],
        *,
        max_concurrency: int,
    ) -> CoordinatedPlan:
        if not 2 <= len(specs) <= 20:
            raise InvalidTaskInput("A coordinated plan requires 2 to 20 Subtasks")
        if not 1 <= max_concurrency <= 10:
            raise InvalidTaskInput("Coordinated max_concurrency must be between 1 and 10")
        by_key = {spec.key: spec for spec in specs}
        if len(by_key) != len(specs):
            raise InvalidTaskInput("Subtask keys must be unique")
        edge_count = sum(len(spec.depends_on) for spec in specs)
        if edge_count > 100:
            raise InvalidTaskInput("A coordinated plan supports at most 100 dependencies")
        for spec in specs:
            missing = set(spec.depends_on) - set(by_key)
            if missing:
                raise InvalidTaskInput(
                    f"Subtask {spec.key} references missing dependencies: "
                    f"{', '.join(sorted(missing))}"
                )
            if spec.key in spec.depends_on:
                raise InvalidTaskInput(f"Subtask {spec.key} cannot depend on itself")
        cls._require_acyclic(specs)
        canonical = json.dumps(
            {
                "version": 1,
                "max_concurrency": max_concurrency,
                "subtasks": [spec.to_dict() for spec in specs],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return cls(
            version=1,
            digest=f"sha256:{sha256(canonical.encode()).hexdigest()}",
            max_concurrency=max_concurrency,
            specs=specs,
        )

    @staticmethod
    def _require_acyclic(specs: tuple[SubtaskSpec, ...]) -> None:
        remaining = {spec.key: set(spec.depends_on) for spec in specs}
        ready = sorted(key for key, dependencies in remaining.items() if not dependencies)
        visited = 0
        while ready:
            key = ready.pop(0)
            visited += 1
            for successor, dependencies in remaining.items():
                if key not in dependencies:
                    continue
                dependencies.remove(key)
                if not dependencies:
                    ready.append(successor)
                    ready.sort()
        if visited != len(specs):
            raise InvalidTaskInput("Coordinated plan must be acyclic")

    def materialize(self, task_id: UUID) -> tuple[list[Subtask], list[SubtaskDependency]]:
        ids = {spec.key: uuid4() for spec in self.specs}
        subtasks = [
            Subtask.create(
                subtask_id=ids[spec.key],
                task_id=task_id,
                key=spec.key,
                objective=spec.objective,
                input=spec.input,
                required_capabilities=spec.required_capabilities,
                preferred_agent_id=spec.preferred_agent_id,
                initially_ready=not spec.depends_on,
            )
            for spec in self.specs
        ]
        dependencies = [
            SubtaskDependency(
                task_id=task_id,
                predecessor_id=ids[dependency],
                successor_id=ids[spec.key],
            )
            for spec in self.specs
            for dependency in spec.depends_on
        ]
        return subtasks, dependencies


@dataclass
class Subtask:
    id: UUID
    task_id: UUID
    key: str
    objective: str
    input: dict[str, Any]
    required_capabilities: tuple[str, ...]
    preferred_agent_id: str | None
    status: SubtaskStatus
    current_run_id: UUID | None
    output: dict[str, Any] | None
    error: str | None
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        subtask_id: UUID,
        task_id: UUID,
        key: str,
        objective: str,
        input: dict[str, Any],
        required_capabilities: tuple[str, ...],
        preferred_agent_id: str | None,
        initially_ready: bool,
    ) -> Subtask:
        now = utc_now()
        return cls(
            id=subtask_id,
            task_id=task_id,
            key=key,
            objective=objective,
            input=dict(input),
            required_capabilities=tuple(required_capabilities),
            preferred_agent_id=preferred_agent_id,
            status=SubtaskStatus.READY if initially_ready else SubtaskStatus.BLOCKED,
            current_run_id=None,
            output=None,
            error=None,
            version=1,
            created_at=now,
            updated_at=now,
        )

    def mark_ready(self) -> None:
        if self.status == SubtaskStatus.READY:
            return
        self._require_status(SubtaskStatus.BLOCKED, "mark ready")
        self.status = SubtaskStatus.READY
        self._touch()

    def queue(self, run_id: UUID) -> None:
        self._require_status(SubtaskStatus.READY, "queue")
        if self.current_run_id is not None:
            raise InvalidTaskTransition(f"Subtask {self.id} already has a Run")
        self.current_run_id = run_id
        self._touch()

    def start(self, run_id: UUID) -> None:
        self._require_current_run(run_id)
        self._require_status(SubtaskStatus.READY, "start")
        self.status = SubtaskStatus.RUNNING
        self._touch()

    def complete(self, run_id: UUID, output: dict[str, Any]) -> None:
        self._require_current_run(run_id)
        self._require_status(SubtaskStatus.RUNNING, "complete")
        self.status = SubtaskStatus.COMPLETED
        self.output = dict(output)
        self.error = None
        self._touch()

    def fail(self, run_id: UUID, error: str) -> None:
        self._require_current_run(run_id)
        self._require_status(SubtaskStatus.RUNNING, "fail")
        normalized = error.strip()
        if not normalized:
            raise InvalidTaskInput("Subtask failure must include an error summary")
        self.status = SubtaskStatus.FAILED
        self.output = None
        self.error = normalized
        self._touch()

    def cancel(self) -> None:
        if self.status in TERMINAL_SUBTASK_STATUSES:
            return
        self.status = SubtaskStatus.CANCELED
        self._touch()

    def _require_status(self, expected: SubtaskStatus, action: str) -> None:
        if self.status != expected:
            raise InvalidTaskTransition(
                f"Cannot {action} Subtask {self.id} from status {self.status.value}"
            )

    def _require_current_run(self, run_id: UUID) -> None:
        if self.current_run_id != run_id:
            raise InvalidTaskTransition(f"Run {run_id} is not active for Subtask {self.id}")

    def _touch(self) -> None:
        self.version += 1
        self.updated_at = utc_now()


@dataclass(frozen=True)
class SubtaskDependency:
    task_id: UUID
    predecessor_id: UUID
    successor_id: UUID
