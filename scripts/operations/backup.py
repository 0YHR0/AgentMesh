"""Create a verifiable single-team AgentMesh backup from the Compose deployment."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="backups")
    parser.add_argument("--artifact-dir", default=".agentmesh/artifacts")
    args = parser.parse_args()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = Path(args.output).resolve() / f"agentmesh-{timestamp}"
    target.mkdir(parents=True, exist_ok=False)

    database = target / "postgres.dump"
    command = [
        "docker",
        "compose",
        "exec",
        "-T",
        "postgres",
        "pg_dump",
        "-U",
        "agentmesh",
        "-d",
        "agentmesh",
        "-Fc",
    ]
    database.write_bytes(subprocess.run(command, check=True, capture_output=True).stdout)

    archive = target / "artifacts.zip"
    artifact_dir = Path(args.artifact_dir).resolve()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        if artifact_dir.exists():
            for path in sorted(artifact_dir.rglob("*")):
                if path.is_file():
                    output.write(path, path.relative_to(artifact_dir))

    manifest = {
        "schema": "agentmesh.backup.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": {
            database.name: {"sha256": digest(database), "bytes": database.stat().st_size},
            archive.name: {"sha256": digest(archive), "bytes": archive.stat().st_size},
        },
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(target)


if __name__ == "__main__":
    main()
