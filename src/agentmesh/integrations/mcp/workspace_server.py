from __future__ import annotations

import mimetypes
import os
from hashlib import sha256
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

SERVER_NAME = "agentmesh-workspace"
TOOL_NAME = "read_text"
INPUT_SCHEMA = {
    "properties": {"path": {"title": "Path", "type": "string"}},
    "required": ["path"],
    "title": "read_textArguments",
    "type": "object",
}

mcp = FastMCP(
    SERVER_NAME,
    instructions="Read UTF-8 text files from one configured workspace root.",
)


def _workspace_root() -> Path:
    return Path(os.environ.get("AGENTMESH_MCP_WORKSPACE_ROOT", ".")).resolve(strict=True)


def _max_bytes() -> int:
    try:
        value = int(os.environ.get("AGENTMESH_MCP_WORKSPACE_MAX_BYTES", "65536"))
    except ValueError as exc:
        raise ToolError("Workspace byte limit is invalid") from exc
    if value < 1:
        raise ToolError("Workspace byte limit must be positive")
    return value


def read_workspace_text(path: str) -> dict[str, Any]:
    if not path or "\x00" in path:
        raise ToolError("path must be a non-empty relative path")
    relative_path = Path(path)
    if relative_path.is_absolute():
        raise ToolError("absolute paths are not allowed")

    root = _workspace_root()
    try:
        target = (root / relative_path).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ToolError("requested file was not found") from exc
    if not target.is_relative_to(root) or not target.is_file():
        raise ToolError("requested path is outside the workspace or is not a file")

    limit = _max_bytes()
    size = target.stat().st_size
    if size > limit:
        raise ToolError(f"requested file is {size} bytes; maximum is {limit} bytes")
    with target.open("rb") as stream:
        content_bytes = stream.read(limit + 1)
    if len(content_bytes) > limit:
        raise ToolError(f"requested file exceeds the maximum of {limit} bytes")
    try:
        content = content_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError("requested file is not valid UTF-8 text") from exc

    normalized_path = target.relative_to(root).as_posix()
    media_type = mimetypes.guess_type(normalized_path)[0] or "text/plain"
    return {
        "path": normalized_path,
        "media_type": media_type,
        "size_bytes": len(content_bytes),
        "sha256": sha256(content_bytes).hexdigest(),
        "content": content,
    }


@mcp.tool(
    name=TOOL_NAME,
    description="Read one UTF-8 text file below the configured workspace root.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    structured_output=True,
)
def read_text(path: str) -> dict[str, Any]:
    return read_workspace_text(path)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
