"""Resolve the minds desktop app version + git SHA for Sentry release tagging.

The Electron launcher (``electron/backend.js``) passes both values to the
Python backend via environment variables on every spawn -- both for dev runs
(``just minds-start`` -> ``pnpm start`` -> ``electron .``) and for packaged
builds. The launcher is the source of truth:

* ``MINDS_RELEASE_ID`` -- the desktop app version from ``package.json``.
* ``MINDS_GIT_SHA`` -- the git SHA the code was cut from: resolved live from
  the checkout in dev, and baked into ``electron/build-info.json`` at build
  time for packaged builds.

The fallbacks below only matter for a bare ``uv run minds run`` started without
the launcher: ``release_id`` is read from the in-repo ``package.json``, and the
git SHA degrades to ``"unknown"`` (product code does not shell out to git).
"""

import json
import os
from functools import cache
from pathlib import Path
from typing import Final

from loguru import logger

RELEASE_ID_ENV_VAR = "MINDS_RELEASE_ID"
GIT_SHA_ENV_VAR = "MINDS_GIT_SHA"

UNKNOWN_RELEASE_ID = "0.0.0+unknown"
UNKNOWN_GIT_SHA = "unknown"

# The default-workspace-template release this app build is pinned to: an
# annotated DEFAULT_WORKSPACE_TEMPLATE tag, so a shipped binary clones the
# exact template snapshot it was verified against. Bumped by the release
# process (see docs/deploy/release.md); bump to a newer tag only after re-verifying
# launch-to-msg CI against (this binary, the new tag). Lives here (not in the
# desktop client) so deploy-time code (`minds-admin env deploy`) can read it
# without importing the whole desktop client.
FALLBACK_BRANCH: Final[str] = "minds-v0.4.4"

# The canonical repo key the pool bake stamps into row attributes for the
# default workspace template (`host/org/repo`), the default the web-create
# template pin resolves to when a tier's deploy.toml does not override it.
DEFAULT_WEB_TEMPLATE_REPO_KEY: Final[str] = "github.com/imbue-ai/default-workspace-template"


def _source_package_json() -> Path:
    """Path to the desktop app's ``package.json`` in a source checkout.

    ``build_info.py`` lives at ``apps/minds/imbue/minds/build_info.py``, so the
    ``package.json`` is two directories above the ``imbue/minds`` package root.
    Only resolvable when running from source; packaged runs use the env var.
    """
    return Path(__file__).resolve().parents[2] / "package.json"


@cache
def resolve_release_id() -> str:
    """Return the minds desktop app version (``package.json`` ``version``).

    Prefers the value the Electron launcher passes via ``MINDS_RELEASE_ID``;
    falls back to reading the in-repo ``package.json`` for bare source runs.
    """
    from_env = os.environ.get(RELEASE_ID_ENV_VAR)
    if from_env:
        return from_env
    package_json = _source_package_json()
    try:
        version = json.loads(package_json.read_text()).get("version")
    except (OSError, json.JSONDecodeError) as error:
        logger.debug("Could not read minds release id from {}: {}", package_json, error)
        return UNKNOWN_RELEASE_ID
    if isinstance(version, str) and version:
        return version
    return UNKNOWN_RELEASE_ID


@cache
def resolve_git_sha() -> str:
    """Return the git SHA the running code was cut from.

    Comes from the Electron launcher via ``MINDS_GIT_SHA`` (resolved live from
    the checkout in dev, baked into ``build-info.json`` for packaged builds). A
    bare ``uv run minds run`` started without the launcher reports
    ``"unknown"`` rather than shelling out to git from product code.
    """
    return os.environ.get(GIT_SHA_ENV_VAR) or UNKNOWN_GIT_SHA
