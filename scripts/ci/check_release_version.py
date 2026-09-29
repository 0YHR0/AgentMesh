"""Fail a tag build unless its package metadata and release notes agree."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path


def expected_tag(version: str) -> str:
    match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:(a|b|rc)(\d+))?", version)
    if match is None:
        raise ValueError(f"unsupported package version: {version!r}")
    base, phase, number = match.groups()
    label = {"a": "alpha", "b": "beta", "rc": "rc"}.get(phase)
    return f"v{base}-{label}.{number}" if label else f"v{base}"


def package_version(root: Path) -> str:
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    project = re.search(r"(?ms)^\[project\]\s*(.*?)(?=^\[|\Z)", pyproject)
    if project is None:
        raise ValueError("pyproject.toml has no [project] section")
    version = re.search(r'^version\s*=\s*"([^"]+)"\s*$', project.group(1), re.MULTILINE)
    if version is None:
        raise ValueError("[project] has no literal version")
    return version.group(1)


def module_version(root: Path) -> str:
    source = (root / "src" / "agentmesh" / "__init__.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    for node in module.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    raise ValueError("agentmesh.__version__ is not a string literal")


def check(tag: str, root: Path) -> None:
    version = package_version(root)
    expected = expected_tag(version)
    if tag != expected:
        raise ValueError(
            f"tag {tag!r} does not match package version {version!r}; expected {expected!r}"
        )
    if module_version(root) != version:
        raise ValueError("agentmesh.__version__ does not match pyproject.toml")
    notes = root / "docs" / "releases" / f"{tag}.md"
    if not notes.is_file() or not notes.read_text(encoding="utf-8").strip():
        raise ValueError(f"missing release notes: {notes}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: check_release_version.py <tag>")
    try:
        check(sys.argv[1], Path(__file__).resolve().parents[2])
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
