"""Music Studio web workspace registration."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse

from agentmesh.api.console import console_headers
from agentmesh.packs.music_studio.extension import EXTENSION_ID

ASSET_DIRECTORY = Path(__file__).with_name("assets")


def register_music_studio_console(application: FastAPI) -> None:
    """Register scenario-owned UI routes while preserving public URLs."""

    @application.get("/music-studio", include_in_schema=False)
    def music_studio_index(request: Request) -> FileResponse:
        request.app.state.container.extension_runtime.require_loaded(EXTENSION_ID)
        return FileResponse(ASSET_DIRECTORY / "music-studio.html", headers=console_headers())

    @application.get("/console/assets/music-studio.css", include_in_schema=False)
    def music_studio_stylesheet(request: Request) -> FileResponse:
        request.app.state.container.extension_runtime.require_loaded(EXTENSION_ID)
        return FileResponse(
            ASSET_DIRECTORY / "music-studio.css",
            media_type="text/css",
            headers=console_headers(),
        )

    @application.get("/console/assets/music-studio.js", include_in_schema=False)
    def music_studio_script(request: Request) -> FileResponse:
        request.app.state.container.extension_runtime.require_loaded(EXTENSION_ID)
        return FileResponse(
            ASSET_DIRECTORY / "music-studio.js",
            media_type="text/javascript",
            headers=console_headers(),
        )
