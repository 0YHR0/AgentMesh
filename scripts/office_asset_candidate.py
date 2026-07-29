"""Run an optional, bounded AgentMesh Office asset-generation candidate job.

This utility never replaces a checked-in production asset. It writes a candidate
and a provenance manifest for explicit visual review and promotion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def run_pipeline(
    *,
    prompt: str,
    output: Path,
    manifest: Path,
    command: list[str],
    timeout_seconds: float,
    maximum_attempts: int,
) -> bool:
    if not command:
        raise ValueError("a generator command is required")
    if timeout_seconds <= 0 or maximum_attempts <= 0:
        raise ValueError("timeout and attempts must be positive")
    output.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, object] = {
        "schema": "agentmesh.office-asset-candidate.v1",
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "generator": Path(command[0]).name,
        "candidate": str(output),
        "promotion": "manual-review-required",
        "attempts": [],
        "started_at": _timestamp(),
    }
    attempts = record["attempts"]
    assert isinstance(attempts, list)
    for attempt_number in range(1, maximum_attempts + 1):
        started_at = _timestamp()
        environment = {
            **os.environ,
            "AGENTMESH_ASSET_PROMPT": prompt,
            "AGENTMESH_ASSET_OUTPUT": str(output),
        }
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                env=environment,
                text=True,
                timeout=timeout_seconds,
            )
            generated = (
                result.returncode == 0
                and output.is_file()
                and output.stat().st_size > 0
            )
            status = "succeeded" if generated else "failed"
            attempts.append(
                {
                    "attempt": attempt_number,
                    "finished_at": _timestamp(),
                    "return_code": result.returncode,
                    "started_at": started_at,
                    "status": status,
                    "stderr_tail": result.stderr[-500:],
                }
            )
        except subprocess.TimeoutExpired:
            status = "timed_out"
            attempts.append(
                {
                    "attempt": attempt_number,
                    "finished_at": _timestamp(),
                    "started_at": started_at,
                    "status": status,
                }
            )
        record["status"] = status
        _write_manifest(manifest, record)
        if status == "succeeded":
            record["finished_at"] = _timestamp()
            _write_manifest(manifest, record)
            return True
        if attempt_number < maximum_attempts:
            time.sleep(min(2 ** (attempt_number - 1), 8))
    record["finished_at"] = _timestamp()
    _write_manifest(manifest, record)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--timeout", default=45.0, type=float)
    parser.add_argument("--attempts", default=3, type=int)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    command = arguments.command[1:] if arguments.command[:1] == ["--"] else arguments.command
    succeeded = run_pipeline(
        prompt=arguments.prompt_file.read_text(),
        output=arguments.output,
        manifest=arguments.manifest,
        command=command,
        timeout_seconds=arguments.timeout,
        maximum_attempts=arguments.attempts,
    )
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
