import mimetypes
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                    "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
                ),
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )
