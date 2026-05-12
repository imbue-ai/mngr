"""Layer-2 probe for workspace-server recovery.

Layer 1 is the surgical ``tmux kill-window`` + ``touch services.toml`` path
(``restart-workspace-server``). It only fixes the case where the bootstrap
service manager is alive and reconciling but the workspace-server's tmux
window is stuck. Layer 3 is full container restart (``restart-container``),
which fixes the case where the container itself (tmux, bootstrap, ttyd) is
sick.

This module probes container-level signals so the recovery UX can pick
the right initial tier without falling back to a "let's try L1 and time
out 15s later" wait. We run ``tmux list-windows -a -F '#S:#W'`` inside the
agent's host via ``mngr exec``; if the call succeeds and emits at least
one entry naming the bootstrap window (``svc-system_interface``), the
container is alive and L1 will likely work. Otherwise the container is
considered down and the modal should prompt for L3 immediately.

The original spec also called for ``docker inspect`` and ttyd loopback
checks. We skip both for v1:

- ttyd binds to a random port that only the agent knows, so probing from
  the outside isn't free (would require parsing the registration event).
- ``docker inspect`` would need provider-aware routing through the outer
  host, which isn't worth the complexity when the tmux check already
  fails decisively for a down container.

The probe is intentionally synchronous and on-demand: the chrome modal
fires it when it first sees a STUCK transition. Background polling
would burden a wedged backend with extra traffic for no gain.
"""

import os
import subprocess
from enum import Enum
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.mngr.primitives import AgentId

# Match the bootstrap window name used by the L1 restart path so a missing
# window here means "the thing L1 would kick is already gone" -- i.e. L1
# is unlikely to recover anything.
_BOOTSTRAP_WINDOW_NAME: Final[str] = "svc-system_interface"
# How long the probe ``mngr exec`` may take before we declare the container
# unreachable. Short on purpose: a healthy host responds to a no-op tmux
# query in well under a second; anything past a couple of seconds is
# already telling us the container is wedged.
_PROBE_TIMEOUT_SECONDS: Final[float] = 4.0


class Layer2State(str, Enum):
    """Result of a one-shot container-level probe.

    ALIVE: ``mngr exec`` returned 0 and the bootstrap window was present.
    DOWN: ``mngr exec`` failed, timed out, or returned without the
    expected bootstrap window.
    """

    ALIVE = "alive"
    DOWN = "down"


def _build_probe_argv(mngr_binary: str, agent_id: AgentId) -> list[str]:
    """Build the argv for the in-container tmux probe.

    ``tmux list-windows -a`` enumerates every window on every session
    with a format that lets us scan stdout for the bootstrap window.
    ``--quiet`` suppresses mngr's own banner so stdout is just the tmux
    output.
    """
    return [
        mngr_binary,
        "exec",
        str(agent_id),
        f"tmux list-windows -a -F '#S:#W' 2>/dev/null",
        "--timeout",
        str(_PROBE_TIMEOUT_SECONDS),
        "--quiet",
    ]


def probe_layer2(
    *,
    mngr_binary: str,
    mngr_host_dir: Path,
    agent_id: AgentId,
    concurrency_group: ConcurrencyGroup,
) -> Layer2State:
    """Run a one-shot container probe and classify the result.

    Returns ALIVE iff the mngr exec dispatch succeeds and the stdout
    contains a line of the form ``<session>:svc-system_interface``.
    Any other outcome (non-zero exit, timeout, OS error, missing window)
    is DOWN.
    """
    argv = _build_probe_argv(mngr_binary, agent_id)
    env = dict(os.environ)
    env["MNGR_HOST_DIR"] = str(mngr_host_dir)
    try:
        finished = concurrency_group.run_process_to_completion(
            argv,
            timeout=_PROBE_TIMEOUT_SECONDS + 2.0,
            is_checked_after=False,
            env=env,
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ConcurrencyGroupError) as exc:
        logger.debug("Layer-2 probe for {} failed to dispatch: {}", agent_id, exc)
        return Layer2State.DOWN
    if finished.returncode != 0:
        logger.debug(
            "Layer-2 probe for {} exited {}: stderr={!r}",
            agent_id,
            finished.returncode,
            finished.stderr,
        )
        return Layer2State.DOWN
    stdout = finished.stdout or ""
    for line in stdout.splitlines():
        # Each tmux entry is of the form "<session>:<window>"; we want any
        # line whose window field exactly matches the bootstrap name. A
        # substring check would false-positive on a window literally named
        # "x-svc-system_interface-y", which is wildly improbable but still
        # cheap to exclude with an exact split.
        parts = line.strip().split(":", 1)
        if len(parts) == 2 and parts[1] == _BOOTSTRAP_WINDOW_NAME:
            return Layer2State.ALIVE
    return Layer2State.DOWN
