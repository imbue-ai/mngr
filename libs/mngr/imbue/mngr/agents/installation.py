import re
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
_VERSION_PROBE_TIMEOUT_SECONDS: Final[float] = 30.0

# A "version token" in a ``--version`` banner: a maximal run of word chars, dots,
# plus and minus (so "1.2.3", "0.4.10", "1.2.3-rc1", "v2.1.50", "codex-cli" are each
# single tokens). Used to look for the user's pinned string verbatim rather than
# imposing a semver shape on it.
_VERSION_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[\w.+-]+")


def is_pinned_version_present(version_output: str, pinned_version: str) -> bool:
    """Whether ``pinned_version`` appears verbatim as a token in ``--version`` output.

    Transparent on purpose: the user's pinned string is matched as-is against the
    whitespace/paren-delimited tokens of the banner (e.g. "pi 1.2.3",
    "2.1.50 (Claude Code)", "codex-cli 0.138.0"), so any pin the installer accepts
    -- a plain ``X.Y.Z``, a four-component version, or a pre-release like
    ``1.2.3-rc1`` -- verifies correctly without this code knowing the version
    scheme. A leading ``v`` on either side is ignored so "v1.2.3" matches "1.2.3".
    Token equality (not substring) means a pin of "1.2.3" does not match an
    installed "1.2.30".
    """
    pinned = pinned_version.strip()
    if not pinned:
        return False
    candidates = {pinned, pinned.lstrip("v")}
    for token in _VERSION_TOKEN_RE.findall(version_output):
        if token in candidates or token.lstrip("v") in candidates:
            return True
    return False


def verify_pinned_cli_version(
    host: OnlineHostInterface,
    *,
    command: str,
    binary_name: str,
    pinned_version: str,
) -> None:
    """Verify the installed CLI reports ``pinned_version``, erroring on a mismatch.

    Needed because ``ensure_cli_installed`` skips installation when the binary is
    already present, so a pre-existing global install at the wrong version would
    otherwise satisfy a pin silently. Probes ``<command> --version`` and checks
    whether the pinned string is present in the banner (see
    ``is_pinned_version_present`` -- the pin is passed through verbatim, no version
    scheme assumed). Raises ``AgentInstallationError`` when the banner is non-empty
    and lacks the pin. When the probe fails or yields no output (e.g. the CLI is
    absent or has no ``--version``), logs at debug and returns rather than aborting
    provisioning.

    Both stdout and stderr are inspected because not every CLI prints its version
    to stdout -- pi, for one, writes ``--version`` to stderr -- and the streams are
    combined in code rather than via a shell ``2>&1`` so the result does not depend
    on how a given host executes the command.
    """
    probe = f"{command} --version"
    result = host.execute_idempotent_command(probe, timeout_seconds=_VERSION_PROBE_TIMEOUT_SECONDS)
    output = f"{result.stdout}\n{result.stderr}".strip() if result.success else ""
    if not output:
        logger.debug("Could not determine installed {} version; skipping version pin check.", binary_name)
        return
    if is_pinned_version_present(output, pinned_version):
        logger.debug("{} reports pinned version {}", binary_name, pinned_version)
        return
    raise AgentInstallationError(
        f"{binary_name} version mismatch: `{command} --version` reported {output!r}, "
        f"but agent config pins version {pinned_version!r}. "
        f"Re-install {binary_name} with the correct version or update the pinned version in your agent config."
    )


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
