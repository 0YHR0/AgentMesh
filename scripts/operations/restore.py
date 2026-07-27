"""Validate and restore an AgentMesh v1 backup into an isolated/stopped Compose stack."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("backup")
    parser.add_argument("--artifact-dir", default=".agentmesh/artifacts")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm destructive replacement of the target Compose database",
    )
    args = parser.parse_args()
    if not args.yes:
        raise SystemExit("Restore is destructive; inspect the target and pass --yes")

    source = Path(args.backup).resolve()
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != "agentmesh.backup.v1":
        raise SystemExit("Unsupported backup manifest")
    for name, evidence in manifest["files"].items():
        path = source / name
        if not path.is_file() or digest(path) != evidence["sha256"]:
            raise SystemExit(f"Backup integrity check failed: {name}")

    subprocess.run(["docker", "compose", "up", "-d", "postgres"], check=True)
    dump = (source / "postgres.dump").read_bytes()
    subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "pg_restore",
            "-U",
            "agentmesh",
            "-d",
            "agentmesh",
            "--clean",
            "--if-exists",
            "--no-owner",
        ],
        input=dump,
        check=True,
    )

    artifact_dir = Path(args.artifact_dir).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source / "artifacts.zip") as archive:
        for member in archive.infolist():
            target = (artifact_dir / member.filename).resolve()
            if artifact_dir != target and artifact_dir not in target.parents:
                raise SystemExit("Artifact archive contains an unsafe path")
        archive.extractall(artifact_dir)
    subprocess.run(["docker", "compose", "run", "--rm", "migrate"], check=True)
    print("Restore completed; run the documented verification drill before serving traffic")


if __name__ == "__main__":
    main()
