from hashlib import sha256
from uuid import uuid4

import pytest

from agentmesh.domain.artifacts import ArtifactVersion
from agentmesh.domain.errors import (
    ArtifactIntegrityMismatch,
    ArtifactTooLarge,
    InvalidArtifact,
)


def test_inline_json_artifact_is_validated_and_hashed() -> None:
    content = b'{"result":"ok"}'
    version = ArtifactVersion.create_inline(
        artifact_id=uuid4(),
        version_number=1,
        media_type="application/json",
        content=content,
        max_inline_bytes=1024,
        expected_sha256=sha256(content).hexdigest(),
    )

    assert version.size_bytes == len(content)
    assert version.sha256 == sha256(content).hexdigest()
    assert version.content == content


@pytest.mark.parametrize(
    ("media_type", "content"),
    [
        ("application/octet-stream", b"binary"),
        ("application/json", b"not-json"),
        ("text/plain", b"contains\x00nul"),
        ("text/plain", b"\xff"),
    ],
)
def test_inline_artifact_rejects_unsupported_or_unsafe_content(
    media_type: str,
    content: bytes,
) -> None:
    with pytest.raises(InvalidArtifact):
        ArtifactVersion.create_inline(
            artifact_id=uuid4(),
            version_number=1,
            media_type=media_type,
            content=content,
            max_inline_bytes=1024,
        )


def test_inline_artifact_enforces_size_and_integrity() -> None:
    artifact_id = uuid4()
    with pytest.raises(ArtifactTooLarge):
        ArtifactVersion.create_inline(
            artifact_id=artifact_id,
            version_number=1,
            media_type="text/plain",
            content=b"too large",
            max_inline_bytes=3,
        )

    with pytest.raises(ArtifactIntegrityMismatch):
        ArtifactVersion.create_inline(
            artifact_id=artifact_id,
            version_number=1,
            media_type="text/plain",
            content=b"content",
            max_inline_bytes=1024,
            expected_sha256="0" * 64,
        )
