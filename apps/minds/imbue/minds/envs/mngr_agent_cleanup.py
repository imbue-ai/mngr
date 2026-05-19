"""Destroy mngr agents living under an env's MNGR_HOST_DIR.

``minds env destroy`` calls this before tearing down cloud-side
infrastructure: if the env has any active agents, they reference cloud
resources (Docker containers, pool hosts, Modal sandboxes, Cloudflare
tunnels). Tearing down those resources without first destroying the
agents leaves orphan mngr state pointing at dead URLs / containers,
which surfaces later as confusing errors when the operator next
``mngr list``s the env.

The cleanup walks ``<env_root>/mngr/agents/<agent-id>/`` to enumerate
known agent ids, then shells out to ``mngr destroy <agent-id>`` per
agent with the env's MNGR_HOST_DIR / MNGR_PREFIX exported in the
subprocess env so the inner ``mngr`` reads the right host_dir. Pure
subprocess-based to avoid pulling ``imbue.mngr`` into the envs
package's import surface.
"""

import os
from collections.abc import Callable
from pathlib import Path

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.bootstrap import mngr_host_dir_for
from imbue.minds.bootstrap import mngr_prefix_for
from imbue.minds.bootstrap import root_name_for_env_name
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.errors import MindError

_MNGR_DESTROY_TIMEOUT_SECONDS: float = 120.0


class MngrAgentCleanupError(MindError):
    """Raised when ``mngr destroy`` fails for any agent during env teardown."""


# Type alias: (agent_id, mngr_host_dir, mngr_prefix, cg) -> None. The
# CLI side passes the real subprocess wrapper; tests pass a fake.
DestroyMngrAgentFn = Callable[[str, Path, str, ConcurrencyGroup], None]


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
    agents_dir = mngr_host_dir_for(root_name) / "agents"
    if not agents_dir.is_dir():
        return ()
    return tuple(sorted(child.name for child in agents_dir.iterdir() if child.is_dir()))


def destroy_all_mngr_agents_in_env(
    name: DevEnvName,
    *,
    destroy_agent: DestroyMngrAgentFn,
    parent_concurrency_group: ConcurrencyGroup,
) -> int:
    """Destroy every mngr agent under the env's MNGR_HOST_DIR. Returns count.

    Walks ``<env_root>/mngr/agents/`` and runs the injected
    ``destroy_agent`` callable for each agent id, with the env's
    ``MNGR_HOST_DIR`` + ``MNGR_PREFIX`` resolved up-front so the
    callable can set them in the subprocess env.

    Raises :class:`MngrAgentCleanupError` if any agent destroy fails --
    the caller is expected to abort the env teardown so the operator
    can investigate. Without this strictness, a single stuck agent
    would leave its associated cloud resources stranded after the
    cloud-side cleanup runs.
    """
    agent_ids = list_agent_ids_in_env_root(name)
    if not agent_ids:
        return 0

    root_name = root_name_for_env_name(str(name))
    mngr_host_dir = mngr_host_dir_for(root_name)
    mngr_prefix = mngr_prefix_for(root_name)

    failures: list[tuple[str, Exception]] = []
    for agent_id in agent_ids:
        logger.info(
            "Destroying mngr agent {!r} under env {!r} (MNGR_HOST_DIR={})...",
            agent_id,
            str(name),
            mngr_host_dir,
        )
        try:
            destroy_agent(agent_id, mngr_host_dir, mngr_prefix, parent_concurrency_group)
        except (MindError, OSError) as exc:
            failures.append((agent_id, exc))
            logger.error("`mngr destroy {}` failed: {}", agent_id, exc)

    if failures:
        joined = "; ".join(f"{aid}: {exc}" for aid, exc in failures)
        raise MngrAgentCleanupError(
            f"Failed to destroy {len(failures)} of {len(agent_ids)} mngr agent(s) under env "
            f"{str(name)!r}: {joined}. The env root has NOT been removed; re-run "
            "`minds env destroy` once the underlying issue is fixed."
        )

    return len(agent_ids)


def real_destroy_mngr_agent(
    agent_id: str,
    mngr_host_dir: Path,
    mngr_prefix: str,
    parent_concurrency_group: ConcurrencyGroup,
) -> None:
    """Shell out to ``mngr destroy <agent_id>`` with the env's MNGR_* vars set.

    The CLI side wires this into the Providers bundle as the
    ``destroy_mngr_agent`` callable. Subprocess runs under the
    parent ConcurrencyGroup so its lifetime is tracked alongside the
    rest of the destroy flow.
    """
    subprocess_env = dict(os.environ)
    subprocess_env["MNGR_HOST_DIR"] = str(mngr_host_dir)
    subprocess_env["MNGR_PREFIX"] = mngr_prefix
    command = ["mngr", "destroy", agent_id]
    cg = parent_concurrency_group.make_concurrency_group(name=f"mngr-destroy-{agent_id}")
    with cg:
        result = cg.run_process_to_completion(
            command=command,
            timeout=_MNGR_DESTROY_TIMEOUT_SECONDS,
            is_checked_after=False,
            env=subprocess_env,
        )
    if result.returncode == 0:
        return
    # mngr's "no such agent" wording: "Agent <id> not found". Treat
    # that as a no-op (someone already destroyed it manually).
    message = (result.stderr + result.stdout).lower()
    if "not found" in message or "does not exist" in message:
        return
    stderr = result.stderr.strip() or result.stdout.strip()
    raise MngrAgentCleanupError(f"`mngr destroy {agent_id}` failed (exit {result.returncode}): {stderr}")
