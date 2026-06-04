"""Provision the latchkey CLI (and its runtime prerequisites) on a remote VPS.

This is the first piece of "run the latchkey gateway *on* the VPS" support.
Where the rest of the package reverse-tunnels a desktop-side gateway into
each agent, this module installs the upstream ``latchkey`` CLI directly on
the agent's outer host (the VPS) so a gateway can eventually be run there.
"""

import time
from typing import Final

from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr_latchkey.core import LatchkeyError

# Version of the upstream ``latchkey`` CLI to install on the VPS. Pinned so
# every remote gateway runs a known-good release rather than whatever
# ``npm install -g latchkey`` happens to resolve to at install time.
LATCHKEY_VERSION: Final[str] = "2.15.1"

# Major Node.js version installed via NodeSource. The latchkey CLI is an npm
# package, so it needs a reasonably recent Node runtime; the Debian-shipped
# nodejs is too old, hence the NodeSource setup script.
_NODE_MAJOR_VERSION: Final[str] = "24"

# Generous wall-clock ceiling: ``apt-get update`` + a NodeSource install +
# ``npm install -g`` on a cold VPS routinely runs into the low minutes.
_INSTALL_TIMEOUT_SECONDS: Final[float] = 300.0

# If the install round-trip exceeds this, something is degrading (slow apt
# mirror, slow npm registry) even though it eventually succeeded; warn so we
# notice before it turns into an outright timeout.
_SLOW_INSTALL_WARNING_THRESHOLD_SECONDS: Final[float] = 90.0


class RemoteGatewayError(LatchkeyError, RuntimeError):
    """Raised when provisioning the latchkey CLI on a remote VPS fails."""


def _build_ensure_installed_script(latchkey_version: str, node_major_version: str) -> str:
    """Build an idempotent POSIX-sh script that installs curl, Node.js, and latchkey.

    Each component is gated behind a presence check so a re-run on an
    already-provisioned VPS does no install work. The script avoids
    ``pipefail`` (unsupported by Debian's default ``/bin/sh``, dash) by
    downloading the NodeSource setup script to a file instead of piping it,
    so ``set -e`` still aborts on a failed download.
    """
    nodesource_url = f"https://deb.nodesource.com/setup_{node_major_version}.x"
    return "\n".join(
        (
            "set -e",
            "export DEBIAN_FRONTEND=noninteractive",
            # curl is needed to fetch the NodeSource setup script below.
            "if ! command -v curl >/dev/null 2>&1; then",
            "  apt-get update",
            "  apt-get install -y curl",
            "fi",
            # Node.js + npm via NodeSource (Debian's own nodejs is too old).
            "if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then",
            f"  curl -fsSL {nodesource_url} -o /tmp/nodesource_setup.sh",
            "  bash /tmp/nodesource_setup.sh",
            "  apt-get install -y nodejs",
            "  rm -f /tmp/nodesource_setup.sh",
            "fi",
            # latchkey CLI, pinned to the exact version. Reinstall whenever the
            # installed version differs (missing latchkey reports an empty string).
            f'if [ "$(LATCHKEY_DISABLE_COUNTING=1 latchkey --version 2>/dev/null | sed \'s/^v//\')" != "{latchkey_version}" ]; then',
            f"  npm install -g latchkey@{latchkey_version}",
            "fi",
        )
    )


def ensure_latchkey_installed(host: OuterHostInterface) -> None:
    """Ensure curl, Node.js, and the pinned latchkey CLI are installed on the VPS.

    Idempotent: each component is installed only when missing (or, for
    latchkey, when the installed version differs from :data:`LATCHKEY_VERSION`).
    Raises :class:`RemoteGatewayError` if the install fails.
    """
    script = _build_ensure_installed_script(LATCHKEY_VERSION, _NODE_MAJOR_VERSION)
    host_name = host.get_name()
    with log_span("Ensuring latchkey {} is installed on VPS {}", LATCHKEY_VERSION, host_name):
        started_at = time.monotonic()
        result = host.execute_idempotent_command(script, timeout_seconds=_INSTALL_TIMEOUT_SECONDS)
        elapsed_seconds = time.monotonic() - started_at

    if not result.success:
        raise RemoteGatewayError(
            "Failed to install latchkey {} prerequisites on VPS {}: {}".format(
                LATCHKEY_VERSION, host_name, result.stderr.strip() or result.stdout.strip()
            )
        )
    if elapsed_seconds > _SLOW_INSTALL_WARNING_THRESHOLD_SECONDS:
        logger.warning(
            "Installing latchkey prerequisites on VPS {} took {:.0f}s",
            host_name,
            elapsed_seconds,
        )
