import mimetypes
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
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
            headers=console_headers(),
        )

    @application.get("/world", include_in_schema=False)
    @application.get("/world-3d", include_in_schema=False)
    def legacy_office_redirect() -> RedirectResponse:
        """Send retired game-office links to the product console."""

        return RedirectResponse(url="/", status_code=308, headers=console_headers())


def console_headers() -> dict[str, str]:
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


# Kept for callers that used the original private helper during pre-alpha.
_console_headers = console_headers
