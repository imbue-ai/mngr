"""Verify the release Dockerfile's CLAUDE_CODE_VERSION matches the pin in
forever-claude-template's .mngr/settings.toml.

The release sandbox installs claude at image-build time using the Dockerfile's
`ARG CLAUDE_CODE_VERSION`. Agents spawned from forever-claude-template have
their claude agent config pinned via `[agent_types.claude].version` in that
repo's `.mngr/settings.toml`. If the two drift, provisioning fails with
"Claude version mismatch: installed version is X, but agent config pins
version Y." (see `libs/mngr_claude/.../plugin.py::provision`).

This test reads both values and asserts they match.
"""

import base64
import json
import os
import re
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from loguru import logger

_REPO_ROOT = Path(__file__).resolve().parents[4]
_DOCKERFILE_PATH = _REPO_ROOT / "libs" / "mngr" / "imbue" / "mngr" / "resources" / "Dockerfile"
_TEMPLATE_OWNER_REPO = "imbue-ai/forever-claude-template"
_TEMPLATE_SETTINGS_PATH = ".mngr/settings.toml"
_TEMPLATE_CONTENTS_URL = f"https://api.github.com/repos/{_TEMPLATE_OWNER_REPO}/contents/{_TEMPLATE_SETTINGS_PATH}"


def _parse_dockerfile_claude_version(dockerfile_text: str) -> str:
    """Extract the CLAUDE_CODE_VERSION default from a Dockerfile `ARG` line."""
    match = re.search(
        r'^ARG\s+CLAUDE_CODE_VERSION\s*=\s*"([^"]*)"',
        dockerfile_text,
        flags=re.MULTILINE,
    )
    assert match is not None, 'Dockerfile is missing `ARG CLAUDE_CODE_VERSION="..."`.'
    version = match.group(1)
    assert version, (
        "Dockerfile `ARG CLAUDE_CODE_VERSION` default is empty; pin it to match "
        "forever-claude-template's `[agent_types.claude].version`."
    )
    return version


def _fetch_template_claude_version(token: str) -> str | None:
    """Fetch forever-claude-template's pinned claude version via the GitHub contents API.

    Uses the passed-in auth token (forever-claude-template is private). Returns
    the version string on success, or None on any fetch / parse failure so the
    caller can decide whether to skip (offline / no-token) or fail.
    """
    request = urllib.request.Request(
        _TEMPLATE_CONTENTS_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "mngr-claude-version-alignment-test",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError) as e:
        logger.trace("fetch/parse of {} failed: {}", _TEMPLATE_CONTENTS_URL, e)
        return None
    # Guard against non-dict JSON (e.g. GitHub returning a list for a
    # directory endpoint, or a null/string body on an unusual error path).
    # Calling `.get` on a non-dict would raise AttributeError and escape
    # the documented "return None on any parse failure" contract.
    if not isinstance(payload, dict):
        return None
    content_b64 = payload.get("content")
    if not isinstance(content_b64, str):
        return None
    try:
        settings_toml_text = base64.b64decode(content_b64).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as e:
        logger.trace("base64/utf-8 decode of template settings failed: {}", e)
        return None
    # tomllib.loads raises tomllib.TOMLDecodeError (a ValueError subclass) on
    # malformed content. Catch it so callers see the documented "return None on
    # parse failure" behaviour instead of an opaque traceback -- a malformed
    # response means we cannot verify the pin and the test assertion should
    # surface that as its own clearer failure message.
    try:
        parsed = tomllib.loads(settings_toml_text)
    except tomllib.TOMLDecodeError as e:
        logger.trace("tomllib parse of template settings failed: {}", e)
        return None
    try:
        return str(parsed["agent_types"]["claude"]["version"])
    except KeyError as e:
        logger.trace("template settings missing expected key: {}", e)
        return None


@pytest.mark.release
def test_claude_code_version_matches_forever_claude_template_pin() -> None:
    """The Dockerfile's CLAUDE_CODE_VERSION default must match the pin in
    forever-claude-template/.mngr/settings.toml [agent_types.claude].version.

    A mismatch causes the minds desktop-client e2e tests to fail during agent
    provisioning with "Claude version mismatch".
    """
    dockerfile_version = _parse_dockerfile_claude_version(_DOCKERFILE_PATH.read_text())
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        pytest.skip(
            "GITHUB_TOKEN not set; cannot fetch forever-claude-template "
            "(private repo) to verify the pin alignment. Set GITHUB_TOKEN to run."
        )
    template_version = _fetch_template_claude_version(token)
    assert template_version is not None, (
        f"Failed to fetch or parse {_TEMPLATE_CONTENTS_URL}. Check GITHUB_TOKEN "
        "scope (needs repo read access) and template repo reachability."
    )
    assert dockerfile_version == template_version, (
        f"Dockerfile CLAUDE_CODE_VERSION={dockerfile_version!r} does not match "
        f"forever-claude-template's agent_types.claude.version={template_version!r}. "
        f"Bump one of them to match the other. See "
        f"{_DOCKERFILE_PATH} and "
        f"https://github.com/{_TEMPLATE_OWNER_REPO}/blob/main/{_TEMPLATE_SETTINGS_PATH}"
    )
