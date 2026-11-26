"""The console: a small web app for associates and reviewers, served by the API itself.

    /enterprise/v1/console/   intake station, Verify review queue, rules and webhooks

It signs in with an API key (kept in the browser tab's sessionStorage) and calls
the same public endpoints as any integration. Served with a strict Content
Security Policy: scripts and API calls only from this origin, fonts from Google.
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import RedirectResponse, Response

router = APIRouter(include_in_schema=False)

_DIR = Path(__file__).parent / "console"
_FILES = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8",
          "app.css": "text/css; charset=utf-8"}
_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; img-src 'self' data: https:; connect-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-cache",
}


def _serve(name: str) -> Response:
    return Response((_DIR / name).read_bytes(), media_type=_FILES[name], headers=_HEADERS)


@router.get("/console")
def console_redirect() -> RedirectResponse:
    return RedirectResponse("console/", status_code=308)


@router.get("/console/")
def console_index() -> Response:
    return _serve("index.html")


@router.get("/console/app.js")
def console_js() -> Response:
    return _serve("app.js")


@router.get("/console/app.css")
def console_css() -> Response:
    return _serve("app.css")
