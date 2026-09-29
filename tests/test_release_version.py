from pathlib import Path

import pytest

from scripts.ci.check_release_version import check, expected_tag, package_version


@pytest.mark.parametrize(
    ("version", "tag"),
    [
        ("0.2.0a1", "v0.2.0-alpha.1"),
        ("0.2.0a3", "v0.2.0-alpha.3"),
        ("0.2.0b2", "v0.2.0-beta.2"),
        ("0.2.0rc3", "v0.2.0-rc.3"),
        ("0.2.0", "v0.2.0"),
    ],
)
def test_expected_tag(version: str, tag: str) -> None:
    assert expected_tag(version) == tag


def test_current_release_metadata_agrees() -> None:
    root = Path(__file__).resolve().parents[1]
    check(expected_tag(package_version(root)), root)


def test_mismatched_tag_is_rejected() -> None:
    root = Path(__file__).resolve().parents[1]
    with pytest.raises(ValueError, match="does not match package version"):
        check("v0.1.0-alpha.1", root)


def test_missing_notes_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "0.2.0a1"\n', encoding="utf-8"
    )
    module = tmp_path / "src" / "agentmesh" / "__init__.py"
    module.parent.mkdir(parents=True)
    module.write_text('__version__ = "0.2.0a1"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="missing release notes"):
        check("v0.2.0-alpha.1", tmp_path)
