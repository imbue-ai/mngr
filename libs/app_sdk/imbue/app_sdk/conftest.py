import socket
from collections.abc import Generator
from pathlib import Path
from threading import Thread

import pytest
from flask import Flask
from flask import Response
from flask import abort
from flask import current_app
from flask import request
from pydantic import Field
from werkzeug.serving import make_server

from imbue.app_sdk.shell_client import SHELL_URL_ENVIRONMENT_VARIABLE
from imbue.imbue_common.mutable_model import MutableModel

_STAND_IN_KEY = "shell_stand_in"
_NOTES_MANIFEST = """
name = "notes"
display_name = "Notes"
icon = "icon.svg"
description = "Keeps the person's notes and finds them again."
api_schema_path = "/api/schema"
"""


@pytest.fixture
def notes_manifest_path(tmp_path: Path) -> Path:
    """The manifest of a ``notes`` app that describes its API at ``/api/schema``, with the icon it names beside it."""
    manifest = tmp_path / "app.toml"
    manifest.write_text(_NOTES_MANIFEST)
    (tmp_path / "icon.svg").write_text("<svg/>")
    return manifest


class ShellStandIn(MutableModel):
    """A local HTTP server standing in for the workspace shell: what it answers, and what it was asked."""

    url: str = Field(frozen=True, description="The stand-in's base URL")
    body_by_path: dict[str, bytes] = Field(default_factory=dict, description="What each path answers; others are 404")
    requested_paths: list[str] = Field(default_factory=list, description="Every path requested, in order")


def _serve_stand_in_path(requested_path: str) -> Response:
    stand_in: ShellStandIn = current_app.extensions[_STAND_IN_KEY]
    stand_in.requested_paths.append(request.path)
    body = stand_in.body_by_path.get(request.path)
    if body is None:
        abort(404)
    return Response(body, mimetype="application/octet-stream")


@pytest.fixture
def shell_stand_in(monkeypatch: pytest.MonkeyPatch) -> Generator[ShellStandIn, None, None]:
    """A running stand-in for the workspace shell, which ``MINDS_WORKSPACE_SERVER_URL`` points this process at."""
    app = Flask("shell-stand-in", static_folder=None)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    stand_in = ShellStandIn(url=f"http://127.0.0.1:{server.server_port}")
    app.extensions[_STAND_IN_KEY] = stand_in
    app.add_url_rule("/<path:requested_path>", view_func=_serve_stand_in_path)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(SHELL_URL_ENVIRONMENT_VARIABLE, stand_in.url)
    yield stand_in
    server.shutdown()
    thread.join()
    server.server_close()


@pytest.fixture
def unreachable_shell_url(monkeypatch: pytest.MonkeyPatch) -> str:
    """A loopback URL nothing listens on, which ``MINDS_WORKSPACE_SERVER_URL`` points this process at."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv(SHELL_URL_ENVIRONMENT_VARIABLE, url)
    return url
