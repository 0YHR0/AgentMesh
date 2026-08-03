from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Mapping
from pathlib import Path

import pytest

from agentmesh.extensions.installer import inspect_wheel, install_wheel, verify_wheel
from agentmesh.extensions.runtime import RuntimeExtensionRegistry
from agentmesh.extensions.sdk import (
    ExtensionContext,
    ExtensionHealth,
    ExtensionManifest,
    ExtensionWorkspace,
    InvalidRuntimeExtension,
    RuntimeExtensionDefinition,
)
from agentmesh.extensions.trust import ExtensionLock
from agentmesh.features import Feature


def _definition(version: str = "1.2.3") -> RuntimeExtensionDefinition:
    def services(_: ExtensionContext) -> Mapping[str, object]:
        return {"worker": object()}

    return RuntimeExtensionDefinition(
        manifest=ExtensionManifest(
            identifier="example.extension",
            name="Example",
            version=version,
            description="Trusted example",
            required_core_services=("tasks",),
            provided_services=("worker",),
            required_features=(Feature.ARTIFACT_SERVICE.value,),
            workspaces=(ExtensionWorkspace(route="/example", name="Example"),),
        ),
        service_factory=services,
        api_registrar=lambda _: None,
        health_probe=lambda _: ExtensionHealth(status="ready"),
        stop_callback=lambda _: None,
    )


def _write_wheel(path: Path) -> str:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "example_extension-1.2.3.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: example-extension\nVersion: 1.2.3\n",
        )
        archive.writestr(
            "example_extension-1.2.3.dist-info/entry_points.txt",
            "[agentmesh.runtime_extensions]\nexample = example.extension:EXTENSION\n",
        )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_lock(path: Path, sha256: str, *, version: str = "1.2.3") -> ExtensionLock:
    path.write_text(
        json.dumps(
            {
                "api_version": "0.1",
                "extensions": [
                    {
                        "id": "example.extension",
                        "version": version,
                        "trust": "local",
                        "source": "https://example.test/releases/v1.2.3",
                        "distribution": "example-extension",
                        "entry_point": "example",
                        "wheel_sha256": sha256,
                        "required_features": [Feature.ARTIFACT_SERVICE.value],
                        "required_credentials": [],
                        "permissions": [],
                        "external_writes_enabled": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return ExtensionLock.load(path)


def test_lock_rejects_incomplete_external_entry(tmp_path: Path) -> None:
    lock_path = tmp_path / "extensions.lock"
    lock_path.write_text('{"api_version":"0.1","extensions":[{}]}', encoding="utf-8")
    with pytest.raises(InvalidRuntimeExtension, match="complete v0.1 field set"):
        ExtensionLock.load(lock_path)


def test_discovery_rejects_unlocked_entry_point_before_import(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loaded = False

    class Distribution:
        name = "unexpected-extension"
        version = "1.2.3"

    class FakeEntryPoint:
        name = "unexpected"
        dist = Distribution()

        @staticmethod
        def load() -> RuntimeExtensionDefinition:
            nonlocal loaded
            loaded = True
            return _definition()

    wheel = tmp_path / "example.whl"
    sha256 = _write_wheel(wheel)
    lock = _write_lock(tmp_path / "extensions.lock", sha256)
    monkeypatch.setattr(
        "agentmesh.extensions.runtime.entry_points", lambda *, group: [FakeEntryPoint()]
    )

    with pytest.raises(InvalidRuntimeExtension, match="not present in extensions.lock"):
        RuntimeExtensionRegistry.discover(lock=lock)
    assert loaded is False


def test_discovery_validates_distribution_and_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Distribution:
        name = "example-extension"
        version = "1.2.3"

    class FakeEntryPoint:
        name = "example"
        dist = Distribution()

        @staticmethod
        def load() -> RuntimeExtensionDefinition:
            return _definition()

    wheel = tmp_path / "example.whl"
    sha256 = _write_wheel(wheel)
    lock = _write_lock(tmp_path / "extensions.lock", sha256)
    monkeypatch.setattr(
        "agentmesh.extensions.runtime.entry_points", lambda *, group: [FakeEntryPoint()]
    )

    registry = RuntimeExtensionRegistry.discover(lock=lock)
    assert registry.trust("example.extension").wheel_sha256 == sha256
    assert registry.trust("example.extension").trust == "local"


def test_wheel_installer_verifies_before_pip_and_writes_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "example_extension-1.2.3-py3-none-any.whl"
    sha256 = _write_wheel(wheel)
    lock_path = tmp_path / "extensions.lock"
    _write_lock(lock_path, sha256)
    receipt = tmp_path / "audit" / "install.jsonl"
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "agentmesh.extensions.installer.subprocess.run",
        lambda command, check: commands.append(command),
    )

    installed = install_wheel(
        wheel,
        lock_path=lock_path,
        extension_id="example.extension",
        receipt_path=receipt,
        actor="test-operator",
    )

    assert installed.identifier == "example.extension"
    assert commands and commands[0][-1] == str(wheel.resolve())
    record = json.loads(receipt.read_text(encoding="utf-8"))
    assert record["actor"] == "test-operator"
    assert record["extension"]["sha256_verified"] == sha256


def test_wheel_hash_mismatch_is_rejected_without_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "example_extension-1.2.3-py3-none-any.whl"
    _write_wheel(wheel)
    lock_path = tmp_path / "extensions.lock"
    lock = _write_lock(lock_path, "0" * 64)
    called = False

    def run(*_: object, **__: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("agentmesh.extensions.installer.subprocess.run", run)

    with pytest.raises(InvalidRuntimeExtension, match="SHA-256"):
        verify_wheel(inspect_wheel(wheel), lock, "example.extension")
    assert called is False
