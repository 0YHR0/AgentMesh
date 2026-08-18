from __future__ import annotations

import ast
from pathlib import Path


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_reference_agent_imports_only_public_runtime_sdk_and_stdlib() -> None:
    root = Path(__file__).parents[2]
    imports = _imports(root / "src/agentmesh/reference_agent/__main__.py")
    assert "agentmesh.runtime_sdk" in imports
    assert not any(
        name.startswith(
            ("agentmesh.application", "agentmesh.domain", "agentmesh.infrastructure", "langgraph")
        )
        for name in imports
    )


def test_subprocess_adapter_does_not_depend_on_framework_or_application_layers() -> None:
    root = Path(__file__).parents[2]
    imports = _imports(root / "src/agentmesh/infrastructure/runtime/subprocess_adapter.py")
    assert "agentmesh.runtime_sdk" in imports
    assert not any(
        name.startswith(("langgraph", "agentmesh.application", "agentmesh.domain"))
        for name in imports
    )
