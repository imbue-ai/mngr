"""Build and read the ``mngr`` commands the app runs *inside* a workspace.

``mngr exec`` runs its COMMAND through a shell in the container, so each of
these is a single shell string, and each needs the same two things.

**Tolerance.** A workspace's ``.mngr/settings.toml`` is versioned with its
template, but the mngr that parses it is installed separately, so the file can
name config that mngr does not know -- an ``[agent_types.<name>]`` section whose
fields belong to a plugin a release adds, say. Strict parsing (mngr's default)
turns that into a hard exit on *every* inner command, including the update that
would install the missing plugin, so the workspace wedges with no way out from
the app. The workspace's own system interface refuses to be strict for the same
reason (``agent_discovery._get_mngr_context``); the app must not be stricter
than the workspace it is trying to repair.

**A readable failure.** An inner command's verdict reaches the app wrapped in
the outer ``mngr exec``'s own ``Command failed on agent ...``, which says
nothing about why, so the diagnosis is the *first* verdict in the stream rather
than the last one :func:`mngr_failure_verdict` reads for un-nested commands.
"""

import shlex
from collections.abc import Sequence
from typing import Final

from imbue.minds.desktop_client.mngr_command import mngr_verdict_block

# Bare ``mngr`` resolves on the container's PATH (set up by ``mngr exec``'s
# source-env prefix); the desktop app's outer binary path does not exist there.
_CONTAINER_MNGR_BINARY: Final[str] = "mngr"

# An env-assignment prefix rather than a ``--setting``: it is read before any
# config is parsed, which is the failure being tolerated.
_TOLERATE_UNKNOWN_CONFIG: Final[str] = "MNGR_ALLOW_UNKNOWN_CONFIG=1"

# Room for a verdict and the hint mngr appends to it, in something the SPA
# renders -- not for a log dump.
FAILURE_DETAIL_MAX_CHARS: Final[int] = 1000


def build_in_workspace_mngr_command(argv: Sequence[str]) -> str:
    """The shell string that runs ``mngr <argv>`` inside a workspace.

    ``argv`` is the argument vector *after* the program name, which this adds:
    naming the binary is the caller's one way to bypass the tolerance above.
    Shell-quoted, so a seed message or a format string cannot break out of its
    own argument.
    """
    return f"{_TOLERATE_UNKNOWN_CONFIG} {shlex.join([_CONTAINER_MNGR_BINARY, *argv])}"


def in_workspace_failure_detail(stderr: str) -> str:
    """The inner command's own verdict from a failed in-workspace run's stderr.

    The *first* marker starts the block, unlike the un-nested case
    :func:`mngr_failure_verdict` reads: the last one here is the outer ``mngr
    exec``'s own ``Command failed on agent ...``. What comes before the first is
    the outer mngr's discovery chatter -- an unreachable host it skipped, a
    duplicate host name -- which reads as the cause but is not.
    """
    return mngr_verdict_block(stderr, is_first_verdict=True, max_chars=FAILURE_DETAIL_MAX_CHARS)
