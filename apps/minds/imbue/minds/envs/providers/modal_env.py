"""Create / delete Modal environments via the ``modal`` CLI.

Modal environments are scopes inside a workspace. Each dynamic dev env
gets its own Modal environment so deployed apps + Modal Secrets are
isolated from other developers' work in the same dev workspace.

We shell out to the local ``modal`` CLI because Modal does not publish a
public Python API for environment management; the CLI is what their docs
sanction (``modal environment create`` / ``modal environment delete``).
Authentication piggybacks on whatever ``modal token set`` / ``MODAL_*``
config the operator already has for the dev tier workspace.
"""

import shutil
import subprocess
from typing import Final

from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.errors import MindError

MODAL_BINARY: Final[str] = "modal"


class ModalEnvProviderError(MindError):
    """Raised when a Modal CLI invocation fails."""


def _run_modal(args: list[str], *, modal_binary: str = MODAL_BINARY) -> str:
    if shutil.which(modal_binary) is None:
        raise ModalEnvProviderError(
            f"`{modal_binary}` CLI not found on PATH. Install it from "
            "https://modal.com/docs/guide/installation and run `modal token set` first."
        )
    command = [modal_binary, *args]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise ModalEnvProviderError(f"Failed to invoke {modal_binary}: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise ModalEnvProviderError(f"`{' '.join(command)}` failed (exit {result.returncode}): {stderr}")
    return result.stdout


def create_modal_env(name: DevEnvName, *, modal_binary: str = MODAL_BINARY) -> None:
    """Create a new Modal environment with the given ``name``.

    Idempotent against an already-existing environment of the same name --
    Modal returns a non-zero exit code with ``already exists`` in stderr,
    which we treat as success.
    """
    try:
        _run_modal(["environment", "create", str(name)], modal_binary=modal_binary)
    except ModalEnvProviderError as exc:
        if "already exists" in str(exc).lower():
            return
        raise


def delete_modal_env(name: DevEnvName, *, modal_binary: str = MODAL_BINARY) -> None:
    """Delete the Modal environment with the given ``name``.

    Idempotent: returns silently if the environment does not exist.
    """
    try:
        _run_modal(
            ["environment", "delete", str(name), "--yes"],
            modal_binary=modal_binary,
        )
    except ModalEnvProviderError as exc:
        if "not found" in str(exc).lower() or "does not exist" in str(exc).lower():
            return
        raise
