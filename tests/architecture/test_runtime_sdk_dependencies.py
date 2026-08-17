"""Keep the canonical Runtime boundary framework-neutral."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

RUNTIME_SDK = Path(__file__).parents[2] / "src" / "agentmesh" / "runtime_sdk"
ALLOWED_EXTERNAL_ROOTS = set(sys.stdlib_module_names) | {"__future__"}


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def test_runtime_sdk_has_no_inward_or_framework_dependencies() -> None:
    imports = {module for path in RUNTIME_SDK.rglob("*.py") for module in imported_modules(path)}
    roots = {module.split(".", 1)[0] for module in imports}
    assert roots <= ALLOWED_EXTERNAL_ROOTS


def test_runtime_sdk_public_modules_are_importable_without_application_bootstrap() -> None:
    import agentmesh.runtime_sdk as sdk

    assert sdk.API_VERSION == 1
    assert sdk.CANONICALIZATION_VERSION == "agentmesh-runtime-jcs-v1"
    assert sdk.ManagedAgentRuntime
