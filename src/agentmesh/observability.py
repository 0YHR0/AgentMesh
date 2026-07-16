from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from types import TracebackType
from typing import Any

from agentmesh.application.ports import AttemptTelemetry
from agentmesh.domain.observability import UsageRecord
from agentmesh.domain.tasks import Task, TaskAttempt, TaskRun

logger = logging.getLogger(__name__)


class NoOpAttemptTelemetry:
    @contextmanager
    def observe_attempt(
        self,
        task: Task,
        run: TaskRun,
        attempt: TaskAttempt,
    ) -> Iterator[None]:
        yield

    def record_usage(self, record: UsageRecord) -> None:
        return None

    def close(self) -> None:
        return None


class LangfuseAttemptTelemetry:
    """Privacy-safe Langfuse adapter.

    It exports identifiers, lifecycle metadata, and usage only. Task objective,
    input, and output remain in AgentMesh's business stores.
    """

    def __init__(self, client: Any, propagate: Any) -> None:
        self._client = client
        self._propagate = propagate

    @contextmanager
    def observe_attempt(
        self,
        task: Task,
        run: TaskRun,
        attempt: TaskAttempt,
    ) -> Iterator[None]:
        observation_context: Any | None = None
        propagation_context: Any | None = None
        observation: Any | None = None
        try:
            propagation_context = self._propagate(
                session_id=str(task.id),
                trace_name="agentmesh-task-attempt",
                tags=["agentmesh", f"agent:{run.agent_id}"],
                metadata={
                    "tenant_key": sha256(task.tenant_id.encode("utf-8")).hexdigest()
                },
            )
            propagation_context.__enter__()
        except Exception:
            self._safe_exit(propagation_context, (None, None, None))
            propagation_context = None
            logger.warning("Langfuse attribute propagation failed", exc_info=True)

        try:
            observation_context = self._client.start_as_current_observation(
                trace_context={"trace_id": attempt.trace_id},
                name="agentmesh-attempt",
                as_type="agent",
                metadata={
                    "task_id": str(task.id),
                    "run_id": str(run.id),
                    "attempt_id": str(attempt.id),
                    "fencing_token": attempt.fencing_token,
                    "agent_id": run.agent_id,
                    "agent_version_id": (
                        str(run.agent_version_id) if run.agent_version_id else None
                    ),
                    "agent_version_digest": run.agent_version_digest,
                },
            )
            observation = observation_context.__enter__()
        except Exception:
            logger.warning("Langfuse attempt setup failed; execution will continue", exc_info=True)
            self._safe_exit(observation_context, (None, None, None))
            self._safe_exit(propagation_context, (None, None, None))
            yield
            return

        error_info: tuple[type[BaseException], BaseException, TracebackType] | tuple[
            None, None, None
        ] = (None, None, None)
        try:
            yield
        except BaseException as exc:
            error_info = sys.exc_info()  # type: ignore[assignment]
            self._safe_update(
                observation,
                level="ERROR",
                status_message=f"AgentMesh workflow failed: {type(exc).__name__}",
            )
            raise
        finally:
            self._safe_exit(observation_context, error_info)
            self._safe_exit(propagation_context, error_info)

    def record_usage(self, record: UsageRecord) -> None:
        try:
            cost_details = None
            if record.currency == "USD":
                cost_details = {
                    key: value / 1_000_000
                    for key, value in record.cost_details_micros.items()
                }
            with self._client.start_as_current_observation(
                name="agentmesh-model-usage",
                as_type="generation",
                model=record.model,
                usage_details=dict(record.usage_details),
                cost_details=cost_details,
                metadata={
                    "usage_record_id": str(record.id),
                    "provider": record.provider,
                    "source": record.source.value,
                    "currency": record.currency,
                    "pricing_version": record.pricing_version,
                    "attempt_id": str(record.attempt_id),
                },
            ):
                pass
        except Exception:
            logger.warning("Langfuse usage export failed; execution will continue", exc_info=True)

    def close(self) -> None:
        try:
            self._client.shutdown()
        except Exception:
            logger.warning("Langfuse shutdown failed", exc_info=True)

    @staticmethod
    def _safe_update(observation: Any | None, **values: Any) -> None:
        if observation is None:
            return
        try:
            observation.update(**values)
        except Exception:
            logger.warning("Langfuse observation update failed", exc_info=True)

    @staticmethod
    def _safe_exit(
        context: Any | None,
        error_info: tuple[type[BaseException], BaseException, TracebackType]
        | tuple[None, None, None],
    ) -> None:
        if context is None:
            return
        try:
            context.__exit__(*error_info)
        except Exception:
            logger.warning("Langfuse observation close failed", exc_info=True)


def create_attempt_telemetry(
    *,
    enabled: bool,
    public_key: str | None = None,
    secret_key: str | None = None,
    base_url: str | None = None,
    environment: str | None = None,
    timeout_seconds: int = 5,
) -> AttemptTelemetry:
    if not enabled:
        return NoOpAttemptTelemetry()

    from langfuse import Langfuse, propagate_attributes

    client = Langfuse(
        public_key=public_key,
        secret_key=secret_key,
        base_url=base_url,
        environment=environment,
        timeout=timeout_seconds,
    )
    return LangfuseAttemptTelemetry(client, propagate_attributes)
