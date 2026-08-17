"""Keep the canonical Runtime boundary framework-neutral."""

from __future__ import annotations

import ast
from pathlib import Path

RUNTIME_SDK = Path(__file__).parents[2] / "src" / "agentmesh" / "runtime_sdk"
FORBIDDEN_ROOTS = {
    "agentmesh.application",
    "agentmesh.domain",
    "agentmesh.infrastructure",
    "agentmesh.workers",
    "fastapi",
    "langchain",
    "langgraph",
    "mcp",
    "redis",
    "sqlalchemy",
}


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_runtime_sdk_has_no_inward_or_framework_dependencies() -> None:
    imports = {module for path in RUNTIME_SDK.glob("*.py") for module in imported_modules(path)}
    forbidden = {
        module
        for module in imports
        if module in FORBIDDEN_ROOTS
        or any(module.startswith(f"{root}.") for root in FORBIDDEN_ROOTS)
    }
    assert not forbidden


def test_runtime_sdk_public_modules_are_importable_without_application_bootstrap() -> None:
    import agentmesh.runtime_sdk as sdk

    assert sdk.API_VERSION == 1
    assert sdk.CANONICALIZATION_VERSION == "agentmesh-runtime-v1"
    assert sdk.ManagedAgentRuntime
