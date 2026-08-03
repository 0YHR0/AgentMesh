"""Music Studio web workspace registration."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from agentmesh.api.console import console_headers

ASSET_DIRECTORY = Path(__file__).with_name("assets")


def register_music_studio_console(application: FastAPI) -> None:
    """Register scenario-owned UI routes while preserving public URLs."""

    @application.get("/music-studio", include_in_schema=False)
    def music_studio_index() -> FileResponse:
        return FileResponse(ASSET_DIRECTORY / "music-studio.html", headers=console_headers())

    @application.get("/console/assets/music-studio.css", include_in_schema=False)
    def music_studio_stylesheet() -> FileResponse:
        return FileResponse(
            ASSET_DIRECTORY / "music-studio.css",
            media_type="text/css",
            headers=console_headers(),
        )

    @application.get("/console/assets/music-studio.js", include_in_schema=False)
    def music_studio_script() -> FileResponse:
        return FileResponse(
            ASSET_DIRECTORY / "music-studio.js",
            media_type="text/javascript",
            headers=console_headers(),
        )
