from typing import Any

from agentmesh.application.ports import AgentExecutionContext
from agentmesh.domain.tasks import AcceptanceCriterion, AcceptanceCriterionKind


class DeterministicAgentExecutor:
    """A zero-credential executor used to prove the platform execution path."""

    def execute(
        self,
        *,
        objective: str,
        input: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        output = {
            "summary": f"Demo agent completed: {objective}",
            "input": dict(input),
            "agent": {
                "id": context.agent_id,
                "version_id": (str(context.agent_version_id) if context.agent_version_id else None),
                "version_digest": context.agent_version_digest,
                "kind": "deterministic-demo",
            },
            "execution": {
                "task_id": str(context.task_id),
                "run_id": str(context.run_id),
                "thread_id": context.thread_id,
            },
        }
        if context.revision_number:
            output["revision"] = {"number": context.revision_number}
        return output


class DeterministicAcceptanceReviewer:
    """Contract evaluator used by the built-in reviewer agent."""

    def execute(
        self,
        *,
        objective: str,
        input: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        candidate = input.get("candidate_output")
        raw_criteria = input.get("acceptance_criteria")
        if not isinstance(candidate, dict) or not isinstance(raw_criteria, list):
            raise TypeError("Reviewer input must contain candidate_output and acceptance_criteria")
        criteria = [AcceptanceCriterion.from_dict(value) for value in raw_criteria]
        results: list[dict[str, Any]] = []
        feedback: list[str] = []
        for criterion in criteria:
            found, actual = self._resolve_path(candidate, criterion.path)
            passed = found
            reason = "Output path exists" if found else "Output path is missing"
            if criterion.kind == AcceptanceCriterionKind.OUTPUT_PATH_EQUALS:
                passed = found and actual == criterion.expected
                reason = "Output value matches" if passed else "Output value does not match"
            results.append({"key": criterion.key, "passed": passed, "reason": reason})
            if criterion.required and not passed:
                feedback.append(f"{criterion.key}: {reason}")
        return {"criteria": results, "feedback": feedback}

    @staticmethod
    def _resolve_path(value: dict[str, Any], path: tuple[str, ...]) -> tuple[bool, Any]:
        current: Any = value
        for segment in path:
            if not isinstance(current, dict) or segment not in current:
                return False, None
            current = current[segment]
        return True, current
