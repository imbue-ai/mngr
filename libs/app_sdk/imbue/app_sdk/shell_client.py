"""Reaching the workspace shell over loopback: where it is, and one bounded GET."""

import http.client
import os
import urllib.error
import urllib.request
from typing import Final

from imbue.app_sdk.errors import ShellUnreachableError

SHELL_URL_ENVIRONMENT_VARIABLE: Final[str] = "MINDS_WORKSPACE_SERVER_URL"
DEFAULT_SHELL_URL: Final[str] = "http://127.0.0.1:8000"
# The shell is on loopback, so an answer that takes longer than this is a shell that is not serving.
SHELL_REQUEST_TIMEOUT_SECONDS: Final[float] = 5.0


def shell_base_url() -> str:
    """The shell's base URL: ``MINDS_WORKSPACE_SERVER_URL`` when the workspace sets it, else the shell's fixed port."""
    return os.environ.get(SHELL_URL_ENVIRONMENT_VARIABLE, DEFAULT_SHELL_URL).rstrip("/")


def fetch_from_shell(path: str) -> bytes:
    """The body the shell answers a GET of ``path`` with; raises ShellUnreachableError for anything but a 200."""
    url = f"{shell_base_url()}{path}"
    try:
        with urllib.request.urlopen(url, timeout=SHELL_REQUEST_TIMEOUT_SECONDS) as response:
            return response.read()
    except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
        raise ShellUnreachableError(f"the workspace shell did not answer {url}: {e}") from e
