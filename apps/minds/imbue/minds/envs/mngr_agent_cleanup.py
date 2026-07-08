"""Destroy mngr agents living under an env's MNGR_HOST_DIR.

``minds env destroy`` calls this before tearing down cloud-side
infrastructure: if the env has any active agents, they reference cloud
resources (Docker containers, pool hosts, Modal sandboxes, Cloudflare
tunnels). Tearing down those resources without first destroying the
agents leaves orphan mngr state pointing at dead URLs / containers,
which surfaces later as confusing errors when the operator next
``mngr list``s the env.

The cleanup walks ``<env_root>/mngr/agents/<agent-id>/`` to enumerate
known agent ids, then shells out to a single ``mngr destroy -f
<agent-id>...`` call with the env's MNGR_HOST_DIR / MNGR_PREFIX
exported in the subprocess env so the inner ``mngr`` reads the right
host_dir. Destroying all ids in one pass also cleans up their
host-mates (and the docker containers + build images those reference)
in one quick call. Pure subprocess-based to avoid pulling ``imbue.mngr``
into the envs package's import surface.
"""

import os
from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.bootstrap import mngr_host_dir_for
from imbue.minds.bootstrap import mngr_prefix_for
from imbue.minds.bootstrap import root_name_for_env_name
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.errors import MindError
from imbue.mngr.hosts.common import get_agents_root_dir

_MNGR_DESTROY_TIMEOUT_SECONDS: float = 120.0


class MngrAgentCleanupError(MindError):
    """Raised when ``mngr destroy`` fails for any agent during env teardown."""


# Type alias: (agent_ids, mngr_host_dir, mngr_prefix, cg) -> None. The
# CLI side passes the real subprocess wrapper; tests pass a fake. All ids
# are destroyed in a single `mngr destroy` call so host-mates (and their
# docker containers + build images) get cleaned up in one quick pass.
DestroyMngrAgentsFn = Callable[[Sequence[str], Path, str, ConcurrencyGroup], None]


def list_agent_ids_in_env_root(name: DevEnvName) -> tuple[str, ...]:
    """Return the agent ids (directory names) under the env's mngr agents dir.

    Mirrors mngr's convention: every agent's persistent state lives at
    ``<MNGR_HOST_DIR>/agents/<agent_id>/``. The dir name *is* the agent
    id; we don't have to load any state to enumerate them.

    Returns an empty tuple when the env root has no mngr profile yet
    (fresh env that never had agents created) so callers can treat
    "no agents" as a clean no-op.
    """
    root_name = root_name_for_env_name(str(name))
    agents_dir = get_agents_root_dir(mngr_host_dir_for(root_name))
    if not agents_dir.is_dir():
        return ()
    return tuple(sorted(child.name for child in agents_dir.iterdir() if child.is_dir()))


def destroy_all_mngr_agents_in_env(
    name: DevEnvName,
    *,
    destroy_agents: DestroyMngrAgentsFn,
    parent_concurrency_group: ConcurrencyGroup,
) -> int:
    """Destroy every mngr agent under the env's MNGR_HOST_DIR. Returns count.

    Walks ``<env_root>/mngr/agents/`` and runs the injected
    ``destroy_agents`` callable ONCE with the full id list, with the
    env's ``MNGR_HOST_DIR`` + ``MNGR_PREFIX`` resolved up-front so the
    callable can set them in the subprocess env. A single ``mngr
    destroy`` call cleans up host-mates (and their docker containers +
    build images) in one quick pass.

    Raises :class:`MngrAgentCleanupError` if the destroy fails -- the
    caller is expected to abort the env teardown so the operator can
    investigate. Without this strictness, a stuck agent would leave its
    associated cloud resources stranded after the cloud-side cleanup
    runs.
    """
    agent_ids = list_agent_ids_in_env_root(name)
    if not agent_ids:
        return 0

    root_name = root_name_for_env_name(str(name))
    mngr_host_dir = mngr_host_dir_for(root_name)
    mngr_prefix = mngr_prefix_for(root_name)

    logger.info(
        "Destroying {} mngr agent(s) under env {!r} (MNGR_HOST_DIR={})...",
        len(agent_ids),
        str(name),
        mngr_host_dir,
    )
    destroy_agents(agent_ids, mngr_host_dir, mngr_prefix, parent_concurrency_group)

    return len(agent_ids)


def real_destroy_mngr_agents(
    agent_ids: Sequence[str],
    mngr_host_dir: Path,
    mngr_prefix: str,
    parent_concurrency_group: ConcurrencyGroup,
) -> None:
    """Shell out to a single ``mngr destroy -f <ids...>`` with the env's MNGR_* vars set.

    The CLI side wires this into the Providers bundle as the
    ``destroy_mngr_agents`` callable. Subprocess runs under the
    parent ConcurrencyGroup so its lifetime is tracked alongside the
    rest of the destroy flow.
    """
    if not agent_ids:
        return
    subprocess_env = dict(os.environ)
    subprocess_env["MNGR_HOST_DIR"] = str(mngr_host_dir)
    subprocess_env["MNGR_PREFIX"] = mngr_prefix
    command = ["mngr", "destroy", "-f", *agent_ids]
    cg = parent_concurrency_group.make_concurrency_group(name="mngr-destroy-batch")
    with cg:
        result = cg.run_process_to_completion(
            command=command,
            timeout=_MNGR_DESTROY_TIMEOUT_SECONDS,
            is_checked_after=False,
            env=subprocess_env,
        )
    if result.returncode == 0:
        return
    # mngr's "no such agent" wording: "Agent <id> not found". Treat a
    # batch that only tripped on already-gone agents as a no-op (someone
    # destroyed them manually).
    message = (result.stderr + result.stdout).lower()
    if "not found" in message or "does not exist" in message:
        return
    stderr = result.stderr.strip() or result.stdout.strip()
    raise MngrAgentCleanupError(
        f"`mngr destroy {' '.join(agent_ids)}` failed (exit {result.returncode}): {stderr}. "
        "The env root has NOT been removed; re-run `minds env destroy` once the underlying "
        "issue is fixed."
    )
