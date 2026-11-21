"""The two browser-side modules the workspace shell builds, served from the app's own origin.

A page talks to the shell framing it through ``app_contract.js``, and its right-click menu is
``context_menu.js``. A module import is a fetch without cookies, which the workspace's forwarder
and the share gateway refuse across origins, so every app serves both files itself. The app
fetches each from the shell once and keeps it for the life of the process. With no shell to
fetch from (the app is running on its own, outside a workspace), both answer 404 and the page
runs without the shell's features.
"""

from threading import Lock
from typing import Final

from flask import Flask
from flask import Response
from flask import abort
from flask import current_app

from imbue.app_sdk.errors import ShellUnreachableError
from imbue.app_sdk.shell_client import fetch_from_shell

SHELL_MODULE_NAMES: Final[tuple[str, ...]] = ("app_contract.js", "context_menu.js")
# The Flask rule the modules are served under, for an app's request guard exemptions.
SHELL_MODULE_RULE: Final[str] = "/_static/<basename>"
# The same route as the app's route table names it.
SHELL_MODULE_ROUTE_KEY: Final[tuple[str, str]] = ("GET", "/_static/{basename}")
SHELL_MODULE_ENDPOINT: Final[str] = "shell_module"
_SHELL_STATIC_PREFIX: Final[str] = "/_static/"
# Each module this process has fetched from the shell, by file name, and the lock held around a fetch so a module
# is fetched once.
_SOURCE_BY_NAME_KEY: Final[str] = "app_sdk_shell_module_sources"
_FETCH_LOCK_KEY: Final[str] = "app_sdk_shell_module_fetch_lock"
_HTTP_NOT_FOUND: Final[int] = 404


def _serve_shell_module(basename: str) -> Response:
    """One of the browser-side modules the workspace shell builds and every app serves from its own origin."""
    # Nothing but the shell-built modules, so the route can never be made to fetch another path.
    if basename not in SHELL_MODULE_NAMES:
        abort(_HTTP_NOT_FOUND)
    source_by_name: dict[str, bytes] = current_app.extensions[_SOURCE_BY_NAME_KEY]
    with current_app.extensions[_FETCH_LOCK_KEY]:
        source = source_by_name.get(basename)
        if source is None:
            try:
                source = fetch_from_shell(f"{_SHELL_STATIC_PREFIX}{basename}")
            except ShellUnreachableError as e:
                current_app.logger.warning("Could not fetch %s from the workspace shell: %s", basename, e)
                abort(_HTTP_NOT_FOUND)
            source_by_name[basename] = source
    return Response(source, mimetype="text/javascript")


def register_shell_module_routes(app: Flask) -> None:
    """Serve ``/_static/app_contract.js`` and ``/_static/context_menu.js`` from ``app``'s own origin."""
    app.extensions[_SOURCE_BY_NAME_KEY] = {}
    app.extensions[_FETCH_LOCK_KEY] = Lock()
    app.add_url_rule(SHELL_MODULE_RULE, endpoint=SHELL_MODULE_ENDPOINT, view_func=_serve_shell_module, methods=["GET"])
