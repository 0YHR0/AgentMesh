from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.parser import BytesParser
from pathlib import Path

from agentmesh.extensions.runtime import ENTRY_POINT_GROUP
from agentmesh.extensions.sdk import InvalidRuntimeExtension
from agentmesh.extensions.trust import ExtensionLock, LockedExtension


@dataclass(frozen=True)
class WheelMetadata:
    path: Path
    sha256: str
    distribution: str
    version: str
    entry_points: dict[str, str]


def inspect_wheel(path: Path) -> WheelMetadata:
    if not path.is_file() or path.suffix != ".whl":
        raise InvalidRuntimeExtension(f"Extension bundle '{path}' must be a readable .whl file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    try:
        with zipfile.ZipFile(path) as archive:
            metadata_files = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            entry_point_files = [
                name for name in archive.namelist() if name.endswith(".dist-info/entry_points.txt")
            ]
            if len(metadata_files) != 1 or len(entry_point_files) != 1:
                raise InvalidRuntimeExtension(
                    "Extension wheel must contain one METADATA and one entry_points.txt file"
                )
            metadata = BytesParser().parsebytes(archive.read(metadata_files[0]))
            parser = configparser.ConfigParser(interpolation=None)
            parser.optionxform = str
            parser.read_string(archive.read(entry_point_files[0]).decode("utf-8"))
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError, configparser.Error) as exc:
        raise InvalidRuntimeExtension(f"Cannot inspect extension wheel '{path}': {exc}") from exc
    distribution = metadata.get("Name", "").strip()
    version = metadata.get("Version", "").strip()
    if not distribution or not version:
        raise InvalidRuntimeExtension("Extension wheel METADATA requires Name and Version")
    entries = dict(parser.items(ENTRY_POINT_GROUP)) if parser.has_section(ENTRY_POINT_GROUP) else {}
    if not entries:
        raise InvalidRuntimeExtension(
            f"Extension wheel does not publish the '{ENTRY_POINT_GROUP}' Entry Point group"
        )
    return WheelMetadata(
        path=path,
        sha256=digest.hexdigest(),
        distribution=distribution,
        version=version,
        entry_points=entries,
    )


def verify_wheel(
    metadata: WheelMetadata,
    lock: ExtensionLock,
    extension_id: str,
) -> LockedExtension:
    expected = lock.get(extension_id)
    if expected.trust == "built-in":
        raise InvalidRuntimeExtension(f"Built-in extension '{extension_id}' cannot be installed")
    checks = (
        ("distribution", expected.distribution, metadata.distribution),
        ("version", expected.version, metadata.version),
        ("wheel SHA-256", expected.wheel_sha256, metadata.sha256),
    )
    for label, wanted, actual in checks:
        if wanted != actual:
            raise InvalidRuntimeExtension(
                f"Extension '{extension_id}' {label} '{actual}' does not match locked value "
                f"'{wanted}'"
            )
    entry_point = expected.entry_point or ""
    if entry_point not in metadata.entry_points:
        raise InvalidRuntimeExtension(
            f"Extension '{extension_id}' does not publish locked Entry Point '{entry_point}'"
        )
    return expected


def install_wheel(
    wheel: Path,
    *,
    lock_path: Path,
    extension_id: str,
    receipt_path: Path,
    actor: str,
    verify_only: bool = False,
    force_reinstall: bool = False,
) -> LockedExtension:
    metadata = inspect_wheel(wheel)
    locked = verify_wheel(metadata, ExtensionLock.load(lock_path), extension_id)
    if verify_only:
        return locked
    command = [sys.executable, "-m", "pip", "install", "--no-deps"]
    if force_reinstall:
        command.append("--force-reinstall")
    command.append(str(wheel.resolve()))
    subprocess.run(command, check=True)
    _append_receipt(receipt_path, actor=actor, locked=locked, metadata=metadata)
    return locked


def _append_receipt(
    path: Path,
    *,
    actor: str,
    locked: LockedExtension,
    metadata: WheelMetadata,
) -> None:
    record = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "actor": actor,
        "action": "extension.install",
        "extension": {
            **asdict(locked),
            "sha256_verified": metadata.sha256,
            "wheel": metadata.path.name,
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify and install an AgentMesh runtime extension wheel"
    )
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--extension-id", required=True)
    parser.add_argument("--lock", type=Path, default=Path("extensions.lock"))
    parser.add_argument(
        "--receipt",
        type=Path,
        default=Path(".agentmesh/extensions/install-audit.jsonl"),
    )
    parser.add_argument("--actor", default="server-admin")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--force-reinstall", action="store_true")
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    try:
        locked = install_wheel(
            arguments.wheel,
            lock_path=arguments.lock,
            extension_id=arguments.extension_id,
            receipt_path=arguments.receipt,
            actor=arguments.actor,
            verify_only=arguments.verify_only,
            force_reinstall=arguments.force_reinstall,
        )
    except (InvalidRuntimeExtension, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Extension installation rejected: {exc}") from exc
    action = "verified" if arguments.verify_only else "installed"
    print(f"Extension '{locked.identifier}' {action} from locked wheel SHA-256")


if __name__ == "__main__":
    main()
