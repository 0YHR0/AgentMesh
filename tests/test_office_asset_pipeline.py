import json
import sys
from pathlib import Path

from scripts.office_asset_candidate import run_pipeline


def test_office_asset_pipeline_records_candidate_without_promoting_it(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate.bin"
    manifest = tmp_path / "candidate.manifest.json"
    command = [
        sys.executable,
        "-c",
        (
            "import os;"
            "open(os.environ['AGENTMESH_ASSET_OUTPUT'], 'wb').write("
            "os.environ['AGENTMESH_ASSET_PROMPT'].encode())"
        ),
    ]

    assert run_pipeline(
        prompt="project-owned pixel campus",
        output=candidate,
        manifest=manifest,
        command=command,
        timeout_seconds=5,
        maximum_attempts=1,
    )
    assert candidate.read_bytes() == b"project-owned pixel campus"
    content = json.loads(manifest.read_text())
    assert content["promotion"] == "manual-review-required"
    assert content["status"] == "succeeded"
    assert content["candidate"] == str(candidate)
