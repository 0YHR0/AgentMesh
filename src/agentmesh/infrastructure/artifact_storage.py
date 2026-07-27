from __future__ import annotations

from pathlib import Path

from agentmesh.domain.errors import InvalidArtifact


class LocalArtifactBlobStore:
    """Content-addressed local store with confined paths and atomic publication."""

    def __init__(self, root: str) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def put(self, *, digest: str, content: bytes) -> str:
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise InvalidArtifact("Artifact blob digest is invalid")
        key = f"sha256/{digest[:2]}/{digest}"
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(content)
            temporary.replace(target)
        return key

    def get(self, storage_key: str) -> bytes:
        target = self._resolve(storage_key)
        try:
            return target.read_bytes()
        except FileNotFoundError as exc:
            raise InvalidArtifact("Artifact blob is unavailable") from exc

    def _resolve(self, storage_key: str) -> Path:
        target = (self._root / storage_key).resolve()
        if target != self._root and self._root not in target.parents:
            raise InvalidArtifact("Artifact storage key escapes the configured root")
        return target
