import shlex
from typing import Final

import click
from loguru import logger

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentInstallationError
from imbue.mngr.interfaces.host import OnlineHostInterface

# Generous ceiling for an install that may download and compile; far above a
# healthy install so a genuinely stuck one fails rather than hanging forever.
_INSTALL_TIMEOUT_SECONDS: Final[float] = 300.0
_CHECK_TIMEOUT_SECONDS: Final[float] = 10.0


def is_binary_present(host: OnlineHostInterface, binary_name: str) -> bool:
    """Return whether ``binary_name`` is resolvable on the host's PATH."""
    result = host.execute_idempotent_command(
        f"command -v {shlex.quote(binary_name)}", timeout_seconds=_CHECK_TIMEOUT_SECONDS
    )
    return result.success


def ensure_cli_installed(
    host: OnlineHostInterface,
    mngr_ctx: MngrContext,
    binary_name: str,
    install_command: str,
) -> None:
    """Ensure ``binary_name`` is installed on the host, installing it if missing.

    Installation is gated: on a local host it auto-installs when ``--yes``, prompts
    when interactive, and otherwise raises with a manual-install hint; on a remote
    host it installs only when ``is_remote_agent_installation_allowed`` is set, else
    raises. Raises ``AgentInstallationError`` if the install fails or the binary is
    still absent afterward.
    """
    if is_binary_present(host, binary_name):
        logger.debug("{} is already installed on the host", binary_name)
        return

    _gate_installation(host, mngr_ctx, binary_name, install_command)

    # The install command's own exit code is the success signal: a fresh install
    # often updates PATH only for future shells (e.g. via ~/.bashrc), so a
    # `command -v` re-check in this shell can spuriously fail. Installers that
    # need a hard check bake it into their command (e.g. claude's `test -x`).
    logger.info("Installing {}...", binary_name)
    result = host.execute_idempotent_command(install_command, timeout_seconds=_INSTALL_TIMEOUT_SECONDS)
    if not result.success:
        raise AgentInstallationError(f"Failed to install {binary_name}. stderr: {result.stderr}")
    logger.info("{} installed successfully", binary_name)


def _gate_installation(
    host: OnlineHostInterface,
    mngr_ctx: MngrContext,
    binary_name: str,
    install_command: str,
) -> None:
    """Decide whether installing is permitted, raising ``AgentInstallationError`` if not."""
    manual_hint = f"{binary_name} is not installed. Install it manually with:\n  {install_command}"
    if host.is_local:
        if mngr_ctx.is_auto_approve:
            logger.debug("Auto-approving {} installation (--yes)", binary_name)
            return
        if mngr_ctx.is_interactive and click.confirm(f"{binary_name} is not installed. Install it now?", default=True):
            return
        raise AgentInstallationError(manual_hint)
    if not mngr_ctx.config.is_remote_agent_installation_allowed:
        raise AgentInstallationError(
            f"{binary_name} is not installed on the remote host and automatic remote installation is disabled. "
            "Set is_remote_agent_installation_allowed = true in your mngr config to enable automatic installation, "
            f"or install {binary_name} manually on the remote host."
        )
    logger.debug("Automatic remote agent installation is enabled for {}", binary_name)
