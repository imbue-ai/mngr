"""Resolve the ``mngr`` executable to invoke as a subprocess.

Foreman shells out to ``mngr`` in two places -- the terminal pty bridge
(``mngr connect``) and the create passthrough (``mngr create`` / ``mngr exec``).
A bare ``"mngr"`` relies on it being on ``PATH``, which is not safe: mngr scrubs
``os.environ["PATH"]`` to a controlled value for agent execution, so a foreman
server launched from a uv/venv (where the ``mngr`` console script lives in
``.venv/bin``) ends up with a PATH that does not contain ``mngr`` -- and the
child ``execvpe("mngr", ...)`` fails with exit 127 (observed as an instantly
closing web terminal).

Resolution order:
1. ``MNGR_FOREMAN_MNGR_BINARY`` env var (explicit override / deployments).
2. ``shutil.which("mngr")`` on the current PATH.
3. ``<dir of the running Python>/mngr`` -- the console script sits next to the
   interpreter that is running foreman (the venv bin dir), so this resolves even
   when PATH has been scrubbed.
4. Fall back to the bare name ``"mngr"`` (last resort).
"""

from __future__ import annotations

import os
import shutil
import sys
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def resolve_mngr_binary() -> str:
    override = os.environ.get("MNGR_FOREMAN_MNGR_BINARY")
    if override:
        return override

    found = shutil.which("mngr")
    if found:
        return found

    sibling = Path(sys.executable).parent / "mngr"
    if sibling.exists():
        return str(sibling)

    return "mngr"
