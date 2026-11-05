"""Viewer-only WSGI endpoint for an authenticated tunnel, bound to loopback."""

from werkzeug.wrappers import Response
from chert.web.dashboard import app as dashboard


def app(environ, start_response):
    path = environ.get("PATH_INFO", "")
    if environ.get("REQUEST_METHOD") not in {"GET", "HEAD"}:
        return Response("Read-only viewer\n", status=405)(environ, start_response)
    if path in {"", "/", "/files"}:
        return Response("", status=302, headers={"Location": "/files/"})(environ, start_response)
    if not path.startswith("/files/"):
        return Response("Not found\n", status=404)(environ, start_response)
    environ["chert.files_only"] = True
    return dashboard(environ, start_response)
