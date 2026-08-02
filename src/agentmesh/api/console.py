import mimetypes
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from agentmesh.features import Feature

CONSOLE_DIRECTORY = Path(__file__).with_name("console_assets")
mimetypes.add_type("text/javascript", ".js")


def register_console(application: FastAPI) -> None:
    """Serve the zero-build operator console with the Control API."""

    application.mount(
        "/console/assets",
        StaticFiles(directory=CONSOLE_DIRECTORY),
        name="console-assets",
    )

    @application.get("/", include_in_schema=False)
    def console_index() -> FileResponse:
        return FileResponse(
            CONSOLE_DIRECTORY / "index.html",
            headers=_console_headers(),
        )

    @application.get("/world", include_in_schema=False)
    def world_index() -> FileResponse:
        return FileResponse(
            CONSOLE_DIRECTORY / "world.html",
            headers=_console_headers(),
        )

    @application.get("/music-studio", include_in_schema=False)
    def music_studio_index() -> FileResponse:
        return FileResponse(
            CONSOLE_DIRECTORY / "music-studio.html",
            headers=_console_headers(),
        )

    @application.get("/world-3d", include_in_schema=False)
    def world_3d_index(request: Request) -> FileResponse:
        request.app.state.container.feature_gates.require(Feature.OFFICE_3D)
        return FileResponse(
            CONSOLE_DIRECTORY / "world3d.html",
            headers=_console_headers(),
        )


def _console_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }
