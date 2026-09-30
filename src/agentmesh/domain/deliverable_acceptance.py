"""Deterministic, explicitly configured checks on a pinned business deliverable."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from agentmesh.domain.errors import InvalidTaskInput
from agentmesh.domain.tasks import AcceptanceCriterion, AcceptanceCriterionKind

ACCEPTANCE_POLICY_INPUT_KEY = "agentmesh_deliverable_acceptance"


def json_digest(value: Any) -> str:
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False
        ).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise InvalidTaskInput("Acceptance evidence must be finite JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
        raise InvalidTaskInput(f"{label} must be a bounded non-empty string")
    value = value.strip()
    if any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise InvalidTaskInput(f"{label} cannot contain control characters or invalid Unicode")
    return value


def _number(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError("invalid_number")
    if len(str(value)) > 64:
        raise ValueError("invalid_number")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid_number") from exc
    magnitude = result.copy_abs()
    if not result.is_finite() or magnitude > Decimal("1e18"):
        raise ValueError("nonfinite_or_out_of_range_number")
    if result != 0 and magnitude < Decimal("1e-18"):
        raise ValueError("nonfinite_or_out_of_range_number")
    return result


def _path(value: Any) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise InvalidTaskInput("Check paths require 1 to 16 segments")
    return [_text(part, "Path segment", 128) for part in value]


def _reference(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"source", "path", "unit"}:
        raise InvalidTaskInput("Quantity reference requires source, path and unit")
    source = value["source"]
    if not isinstance(source, str) or source not in {"TASK_INPUT", "DELIVERABLE"}:
        raise InvalidTaskInput("Unknown quantity source")
    path = _path(value["path"])
    if source == "TASK_INPUT" and path[0].startswith("agentmesh_"):
        raise InvalidTaskInput("Quantity references cannot read reserved platform metadata")
    return {"source": source, "path": path, "unit": _text(value["unit"], "Unit", 64)}


def normalize_acceptance_policy(
    value: Any,
    *,
    target_subtask_key: str,
) -> dict[str, Any]:
    target_subtask_key = _text(target_subtask_key, "Acceptance target", 128)
    if not isinstance(value, dict) or set(value) - {
        "version",
        "checks",
        "require_human_review",
        "target_subtask_key",
    }:
        raise InvalidTaskInput("Unknown acceptance policy fields")
    if type(value.get("version", 1)) is not int or value.get("version", 1) != 1:
        raise InvalidTaskInput("Unsupported acceptance policy version")
    if value.get("target_subtask_key", target_subtask_key) != target_subtask_key:
        raise InvalidTaskInput("Acceptance target must match the primary deliverable")
    human = value.get("require_human_review", False)
    if not isinstance(human, bool):
        raise InvalidTaskInput("Human review must be a boolean")
    checks = value.get("checks")
    if not isinstance(checks, list) or not 1 <= len(checks) <= 20:
        raise InvalidTaskInput("Acceptance requires 1 to 20 checks")
    normalized = []
    keys: set[str] = set()
    for raw in checks:
        if not isinstance(raw, dict):
            raise InvalidTaskInput("Checks must be objects")
        key = _text(raw.get("key"), "Check key", 128)
        description = _text(raw.get("description"), "Check description", 2000)
        required = raw.get("required", True)
        if not isinstance(required, bool) or key in keys:
            raise InvalidTaskInput("Check keys must be unique with boolean required")
        keys.add(key)
        kind = raw.get("kind")
        base = {"key": key, "description": description, "required": required, "kind": kind}
        if kind in ("OUTPUT_PATH_EXISTS", "OUTPUT_PATH_EQUALS"):
            allowed = {"key", "description", "required", "kind", "path"}
            if kind == "OUTPUT_PATH_EQUALS":
                allowed.add("expected")
                if "expected" not in raw:
                    raise InvalidTaskInput("Equality check requires expected")
            if set(raw) - allowed:
                raise InvalidTaskInput("Unknown field check options")
            criterion = AcceptanceCriterion.create(
                key=key,
                description=description,
                required=required,
                kind=AcceptanceCriterionKind(kind),
                path=_path(raw.get("path")),
                expected=deepcopy(raw.get("expected")),
            )
            normalized.append(criterion.to_dict())
        elif kind == "RATE_THRESHOLD":
            if set(raw) - {
                "key",
                "description",
                "required",
                "kind",
                "numerator",
                "denominator",
                "scale",
                "operator",
                "threshold",
                "claim",
                "tolerance",
            }:
                raise InvalidTaskInput("Unknown rate check options")
            numerator = _reference(raw.get("numerator"))
            denominator = _reference(raw.get("denominator"))
            operator = raw.get("operator")
            if not isinstance(operator, str) or operator not in {"LTE", "GTE"}:
                raise InvalidTaskInput("Rate operator must be LTE or GTE")
            try:
                scale = _number(raw.get("scale", 100))
                threshold = _number(raw.get("threshold"))
                tolerance = _number(raw.get("tolerance", "0.001"))
            except ValueError as exc:
                raise InvalidTaskInput("Rate configuration requires finite numbers") from exc
            if not 0 < scale <= 10000 or threshold < 0 or not 0 <= tolerance <= 1:
                raise InvalidTaskInput("Rate configuration is out of range")
            base.update(
                numerator=numerator,
                denominator=denominator,
                operator=operator,
                scale=str(scale),
                threshold=str(threshold),
                tolerance=str(tolerance),
            )
            if "claim" in raw:
                claim = _reference(raw["claim"])
                if (
                    claim["source"] != "DELIVERABLE"
                    or claim["unit"] != f"{numerator['unit']}/{denominator['unit']}"
                ):
                    raise InvalidTaskInput("Claim must reference the deliverable ratio unit")
                base["claim"] = claim
            normalized.append(base)
        else:
            raise InvalidTaskInput("Unknown acceptance check kind")
    if not any(check["required"] for check in normalized):
        raise InvalidTaskInput("Acceptance requires at least one required check")
    result = {
        "version": 1,
        "target_subtask_key": target_subtask_key,
        "require_human_review": human,
        "checks": normalized,
    }
    json_digest(result)
    if len(json.dumps(result, ensure_ascii=False).encode()) > 32000:
        raise InvalidTaskInput("Acceptance policy exceeds size limit")
    return deepcopy(result)


def resolve_path(value: Any, path: list[str]) -> tuple[bool, Any]:
    for part in path:
        if not isinstance(value, dict) or part not in value:
            return False, None
        value = value[part]
    return True, value


def _strict_json_equal(actual: Any, expected: Any) -> bool:
    if type(actual) is not type(expected):
        return False
    try:
        return json_digest(actual) == json_digest(expected)
    except InvalidTaskInput:
        return False


def _quantity(ref: dict[str, Any], task_input: dict, output: dict) -> tuple[Decimal | None, str]:
    source = task_input if ref["source"] == "TASK_INPUT" else output
    found, raw = resolve_path(source, ref["path"])
    if not found or raw is None:
        return None, "missing_quantity"
    if not isinstance(raw, dict) or "unit" not in raw:
        return None, "invalid_quantity"
    if raw["unit"] != ref["unit"]:
        return None, "unit_mismatch"
    if "value" not in raw or raw["value"] is None:
        return None, "missing_quantity"
    try:
        return _number(raw["value"]), "ok"
    except ValueError as exc:
        return None, str(exc)


def evaluate_checks(policy: dict, task_input: dict, output: dict) -> list[dict[str, Any]]:
    results = []
    for check in policy["checks"]:
        result = {
            "key": check["key"],
            "description": check["description"],
            "required": check["required"],
            "status": "PASS",
            "reason": "passed",
            "evidence": {},
        }
        if check["kind"] != "RATE_THRESHOLD":
            found, actual = resolve_path(output, check["path"])
            result["evidence"] = {"path": check["path"], "found": found}
            if not found or actual is None:
                result.update(status="UNKNOWN", reason="missing_output_field")
            elif check["kind"] == "OUTPUT_PATH_EQUALS" and not _strict_json_equal(
                actual, check["expected"]
            ):
                result.update(status="FAIL", reason="unexpected_output_value")
            results.append(result)
            continue
        numerator, n_reason = _quantity(check["numerator"], task_input, output)
        denominator, d_reason = _quantity(check["denominator"], task_input, output)
        evidence = {
            "numerator": check["numerator"],
            "denominator": check["denominator"],
            "numerator_reason": n_reason,
            "denominator_reason": d_reason,
            "scale": check["scale"],
            "operator": check["operator"],
            "threshold": check["threshold"],
        }
        result["evidence"] = evidence
        if numerator is None or denominator is None:
            invalid = [
                reason
                for reason in (n_reason, d_reason)
                if reason not in {"ok", "missing_quantity"}
            ]
            result.update(
                status="FAIL" if invalid else "UNKNOWN",
                reason=invalid[0] if invalid else "missing_quantity",
            )
        elif numerator < 0 or denominator <= 0:
            result.update(status="FAIL", reason="invalid_rate_inputs")
        else:
            with localcontext() as context:
                context.prec = 40
                calculated = numerator / denominator * Decimal(check["scale"])
            evidence.update(
                numerator_value=str(numerator),
                denominator_value=str(denominator),
                calculated=str(calculated),
            )
            threshold = Decimal(check["threshold"])
            passed = (
                calculated <= threshold if check["operator"] == "LTE" else calculated >= threshold
            )
            if not passed:
                result.update(status="FAIL", reason="rate_threshold_not_met")
            if "claim" in check:
                claim, reason = _quantity(check["claim"], task_input, output)
                if claim is None:
                    if reason != "missing_quantity" or result["status"] != "FAIL":
                        result.update(
                            status="UNKNOWN" if reason == "missing_quantity" else "FAIL",
                            reason=reason,
                        )
                else:
                    _, raw_claim = resolve_path(output, check["claim"]["path"])
                    try:
                        claim_scale = _number(raw_claim.get("scale"))
                    except ValueError:
                        claim_scale = None
                    if claim < 0:
                        result.update(status="FAIL", reason="invalid_claimed_rate")
                    elif claim_scale != Decimal(check["scale"]):
                        result.update(status="FAIL", reason="claim_scale_mismatch")
                    elif abs(claim - calculated) > Decimal(check["tolerance"]):
                        result.update(status="FAIL", reason="claimed_rate_mismatch")
                    evidence["claimed"] = str(claim)
        results.append(result)
    return results
