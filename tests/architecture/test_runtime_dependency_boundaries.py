"""Keep framework-specific runtime code at the outward adapter boundary."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
APPLICATION = ROOT / "src" / "agentmesh" / "application"
SDK = ROOT / "src" / "agentmesh" / "runtime_sdk"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_application_runtime_coordinator_is_framework_neutral() -> None:
    forbidden = {
        "langgraph",
        "langchain",
        "celery",
        "temporalio",
        "ray",
        "prefect",
    }
    files = tuple(APPLICATION.rglob("*.py"))
    violations = {
        f"{path.relative_to(ROOT)} imports {module}"
        for path in files
        for module in _imports(path)
        if module.split(".", 1)[0] in forbidden
        or module == "agentmesh.infrastructure.runtime"
        or module.startswith("agentmesh.infrastructure.runtime.")
    }
    assert not violations


def test_runtime_sdk_has_no_inward_or_framework_dependencies() -> None:
    forbidden_prefixes = (
        "agentmesh.application",
        "agentmesh.infrastructure",
        "agentmesh.api",
        "sqlalchemy",
        "fastapi",
        "redis",
        "langgraph",
        "langchain",
    )
    violations = {
        f"{path.relative_to(ROOT)} imports {module}"
        for path in SDK.rglob("*.py")
        for module in _imports(path)
        if module.startswith(forbidden_prefixes)
    }
    assert not violations
