"""Verify the latchkey CLI bundled with the Electron app satisfies mngr_latchkey's floor.

The Electron shell ships its own copy of the upstream ``latchkey`` CLI (the
``latchkey`` entry under ``dependencies`` in ``apps/minds/package.json``) and
hands its path to ``minds run`` via ``MINDS_LATCHKEY_BINARY``. ``minds run``
then calls ``Latchkey.initialize()``, which refuses any binary older than
:data:`imbue.mngr_latchkey.core.LATCHKEY_MIN_VERSION`.

Raising that floor without also bumping the bundled dependency ships an app
that rejects its own latchkey: every startup fails with ``LatchkeyVersionError``.
This test reads both pins and asserts the bundled one is new enough.
"""

import json
import re
from pathlib import Path
from typing import Final

from packaging.version import Version

from imbue.mngr_latchkey.core import LATCHKEY_MIN_VERSION

_PACKAGE_JSON_PATH: Final[Path] = Path(__file__).resolve().parents[2] / "package.json"

# The dependency is pinned as a caret/tilde range (``^3.3.0``, ``~3.3.0``) or as
# an exact version (``3.3.0``); all three resolve to something no older than the
# version they name, which is the floor this test compares against. Any other
# range shape would make that reasoning invalid, so it fails the match instead.
_LATCHKEY_SPECIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[\^~]?(\d+\.\d+\.\d+)$")


def test_bundled_latchkey_is_at_least_the_minimum_version_mngr_latchkey_accepts() -> None:
    """``apps/minds/package.json``'s latchkey pin must be >= ``LATCHKEY_MIN_VERSION``."""
    specifier = json.loads(_PACKAGE_JSON_PATH.read_text())["dependencies"]["latchkey"]
    match = _LATCHKEY_SPECIFIER_PATTERN.match(specifier)
    assert match is not None, (
        f"The latchkey dependency in {_PACKAGE_JSON_PATH} is pinned as {specifier!r}, which is "
        "neither an exact version nor a caret/tilde range; this test cannot tell which binary the "
        "app would bundle. Pin it as ^X.Y.Z, ~X.Y.Z or X.Y.Z."
    )
    bundled_floor = Version(match.group(1))
    minimum = Version(LATCHKEY_MIN_VERSION)
    assert bundled_floor >= minimum, (
        f"The minds app bundles latchkey {specifier}, but mngr_latchkey requires at least "
        f"{LATCHKEY_MIN_VERSION} (LATCHKEY_MIN_VERSION); Latchkey.initialize() would reject the "
        "bundled binary. Bump the package.json dependency (and refresh pnpm-lock.yaml)."
    )
