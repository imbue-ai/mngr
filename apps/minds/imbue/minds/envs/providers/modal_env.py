"""Create / delete Modal environments via the ``modal`` CLI.

Modal environments are scopes inside a workspace. Each dynamic dev env
gets its own Modal environment so deployed apps + Modal Secrets are
isolated from other developers' work in the same dev workspace.

We shell out to the local ``modal`` CLI because Modal does not publish a
public Python API for environment management; the CLI is what their docs
sanction (``modal environment create`` / ``modal environment delete``).

Workspace selection: ``minds env activate`` exports ``MODAL_PROFILE``
from the activated tier's committed ``modal_workspace``, so every modal
shellout (here and in ``per_env_deploy``) targets the correct Modal
account without depending on ``~/.modal.toml``'s ``active = true`` flag.
The operator must have a matching profile entry in ``~/.modal.toml``
(run ``modal token set --profile <workspace>`` once per tier they
operate against).
"""

import shutil
from typing import Final

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.errors import MindError

MODAL_BINARY: Final[str] = "modal"
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 60.0


class ModalEnvProviderError(MindError):
    """Raised when a Modal CLI invocation fails."""


def _run_modal(
    args: list[str],
    *,
    parent_concurrency_group: ConcurrencyGroup,
    modal_binary: str = MODAL_BINARY,
) -> str:
    """Run a one-shot ``modal`` CLI invocation bracketed by ``parent_concurrency_group``."""
    if shutil.which(modal_binary) is None:
        raise ModalEnvProviderError(
            f"`{modal_binary}` CLI not found on PATH. Install it from "
            "https://modal.com/docs/guide/installation and run `modal token set` first."
        )
    command = [modal_binary, *args]
    cg = parent_concurrency_group.make_concurrency_group(name="modal-cli")
    try:
        with cg:
            result = cg.run_process_to_completion(
                command=command,
                timeout=_DEFAULT_TIMEOUT_SECONDS,
                is_checked_after=False,
            )
    except OSError as exc:
        raise ModalEnvProviderError(f"Failed to invoke {modal_binary}: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise ModalEnvProviderError(f"`{' '.join(command)}` failed (exit {result.returncode}): {stderr}")
    return result.stdout


def create_modal_env(
    name: DevEnvName,
    *,
    parent_concurrency_group: ConcurrencyGroup,
    modal_binary: str = MODAL_BINARY,
) -> None:
    """Create a new Modal environment with the given ``name``.

    Idempotent against an already-existing environment of the same name --
    Modal returns a non-zero exit code with ``already exists`` in stderr,
    which we treat as success.
    """
    try:
        _run_modal(
            ["environment", "create", str(name)],
            parent_concurrency_group=parent_concurrency_group,
            modal_binary=modal_binary,
        )
    except ModalEnvProviderError as exc:
        if "already exists" in str(exc).lower():
            return
        raise


def delete_modal_env(
    name: DevEnvName,
    *,
    parent_concurrency_group: ConcurrencyGroup,
    modal_binary: str = MODAL_BINARY,
) -> None:
    """Delete the Modal environment with the given ``name``.

    Idempotent: returns silently if the environment does not exist.
    """
    try:
        _run_modal(
            ["environment", "delete", str(name), "--yes"],
            parent_concurrency_group=parent_concurrency_group,
            modal_binary=modal_binary,
        )
    except ModalEnvProviderError as exc:
        message = str(exc).lower()
        if "not found" in message or "does not exist" in message:
            return
        raise
