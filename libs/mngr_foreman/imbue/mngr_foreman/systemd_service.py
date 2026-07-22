"""Install/uninstall foreman as a systemd service, controlled via systemctl thereafter.

``mngr foreman install`` writes ``/etc/systemd/system/foreman.service`` (Type=simple,
Restart=always) whose ExecStart is the *absolute* path to this mngr binary running
``foreman --host <host> --port <port>``, then runs ``systemctl daemon-reload`` and
``systemctl enable --now foreman``. Privileged steps are run through ``sudo`` when we
are not already root. It is idempotent: re-running rewrites the unit and restarts the
service cleanly. ``mngr foreman uninstall`` stops+disables the service and removes the
unit. This replaces hand-rolled unit files / ``nohup`` / ``-d`` in setup scripts --
after install, foreman is just ``systemctl {start,stop,restart,status} foreman``.

The ExecStart binary is resolved to an absolute path (never a bare ``mngr``) so the
unit does not depend on the service's PATH; a matching ``Environment=PATH`` still
covers the tools foreman shells out to (``ssh``, ``docker``).
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.mngr_foreman.mngr_bin import resolve_mngr_binary

SERVICE_NAME: Final[str] = "foreman"
UNIT_PATH: Final[Path] = Path(f"/etc/systemd/system/{SERVICE_NAME}.service")
# Bound each privileged step so a stuck sudo/systemctl can't hang install forever.
_PRIV_TIMEOUT_SECONDS: Final[float] = 60.0


class ServiceInstallError(Exception):
    """Raised when installing or uninstalling the foreman systemd service fails."""


def render_unit_file(*, user: str, exec_start: str, working_dir: str, path_env: str) -> str:
    """Return the systemd unit-file text for the foreman service (pure)."""
    return (
        "[Unit]\n"
        "Description=foreman web server\n"
        "After=network.target docker.service\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"User={user}\n"
        f"WorkingDirectory={working_dir}\n"
        f"Environment=PATH={path_env}\n"
        # Cap glibc malloc arenas (default = 8 x CPU cores). werkzeug runs a thread per
        # request; each thread otherwise gets its own arena that glibc never returns to
        # the OS, so RSS ratchets to the peak-concurrent footprint. 2 arenas keeps memory
        # low for this low-QPS single-user tool; malloc lock contention is irrelevant here.
        "Environment=MALLOC_ARENA_MAX=2\n"
        f"ExecStart={exec_start}\n"
        "Restart=always\n"
        "RestartSec=3\n"
        # foreman holds many long-lived SSE + WebSocket + SSH-control-master fds at once;
        # the default 1024 soft limit runs out and terminals start failing with
        # "Too many open files". Raise it so an always-on server never hits that wall.
        "LimitNOFILE=1048576\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def resolve_abs_mngr_binary() -> str:
    """Absolute path to the mngr console script (the unit must not rely on PATH)."""
    binary = resolve_mngr_binary()
    path = Path(binary)
    if not path.is_absolute():
        found = shutil.which(binary)
        if found is not None:
            path = Path(found)
    if not path.is_absolute() or not path.exists():
        raise ServiceInstallError(
            f"Could not resolve an absolute mngr binary (got {binary!r}); "
            "set MNGR_FOREMAN_MNGR_BINARY to the mngr path and retry."
        )
    return str(path)


def default_working_dir(mngr_binary: str) -> str:
    """The mngr checkout owning this binary (``…/.venv/bin/mngr`` -> checkout), else $HOME.

    Operates on the given absolute path as-is (no symlink resolution), so the unit
    points at the venv path the caller chose.
    """
    for parent in Path(mngr_binary).parents:
        if parent.name == ".venv":
            return str(parent.parent)
    return str(Path.home())


def default_path_env(mngr_binary: str) -> str:
    """PATH for the unit: the binary's dir + ``~/.local/bin`` + the standard system dirs."""
    entries = [
        str(Path(mngr_binary).parent),
        str(Path.home() / ".local" / "bin"),
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    deduped: list[str] = []
    for entry in entries:
        if entry not in deduped:
            deduped.append(entry)
    return ":".join(deduped)


def build_exec_start(mngr_binary: str, host: str, port: int) -> str:
    """The unit's ExecStart line: this mngr binary running the foreman server."""
    return f"{mngr_binary} {SERVICE_NAME} --host {host} --port {port}"


def _privileged_prefix() -> list[str]:
    """``sudo`` unless we are already root (uid 0)."""
    return [] if os.geteuid() == 0 else ["sudo"]


def _run_privileged(argv: list[str], input_text: str | None = None) -> None:
    """Run one privileged command (via sudo when not root); raise on failure."""
    full = _privileged_prefix() + argv
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv (tee/systemctl/rm), sudo-elevated by design
            full,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=_PRIV_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise ServiceInstallError(f"Could not run {' '.join(full)!r}: {e}") from e
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        hint = " (need root -- run with sudo)" if _privileged_prefix() and "sudo" in detail.lower() else ""
        raise ServiceInstallError(f"Command failed{hint}: {' '.join(full)!r}: {detail}")


def install_service(host: str, port: int) -> str:
    """Write the unit, reload systemd, and enable+start foreman. Returns the unit text."""
    mngr_binary = resolve_abs_mngr_binary()
    unit_text = render_unit_file(
        user=getpass.getuser(),
        exec_start=build_exec_start(mngr_binary, host, port),
        working_dir=default_working_dir(mngr_binary),
        path_env=default_path_env(mngr_binary),
    )
    # ``tee`` writes the root-owned unit from stdin -- works with or without the sudo
    # prefix, and re-running just overwrites it (idempotent).
    _run_privileged(["tee", str(UNIT_PATH)], input_text=unit_text)
    _run_privileged(["systemctl", "daemon-reload"])
    # ``enable`` persists the boot symlink; ``restart`` applies the (possibly updated)
    # unit now, starting it if it was stopped -- so a re-install always takes effect
    # (``enable --now`` would no-op the start of an already-running service).
    _run_privileged(["systemctl", "enable", SERVICE_NAME])
    _run_privileged(["systemctl", "restart", SERVICE_NAME])
    logger.info("Installed foreman systemd service at {}", UNIT_PATH)
    return unit_text


def uninstall_service() -> bool:
    """Stop+disable foreman and remove its unit file. Returns False if it wasn't installed."""
    if not UNIT_PATH.exists():
        return False
    _run_privileged(["systemctl", "disable", "--now", SERVICE_NAME])
    _run_privileged(["rm", "-f", str(UNIT_PATH)])
    _run_privileged(["systemctl", "daemon-reload"])
    logger.info("Removed foreman systemd service ({})", UNIT_PATH)
    return True
