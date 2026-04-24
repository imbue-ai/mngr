"""Private helpers: spawn detached ``latchkey`` subprocesses.

Why this file uses raw ``subprocess.Popen`` (triggering a narrow exclusion
from ``check_direct_subprocess``): we need the spawned processes to *outlive*
the minds desktop client. ``latchkey gateway`` must survive desktop-client
restarts so agents running in containers/VMs keep working. ``latchkey
ensure-browser`` may download Chromium via Playwright, which can take a
while; detaching means we do not block desktop-client shutdown on it and
the next minds session will simply re-check. Either way, the behaviour is
the exact opposite of what ``ConcurrencyGroup`` is built for -- it
guarantees that every spawned process is cleaned up when the group exits.

Confining the ``Popen`` calls to this tiny helper keeps the ratchet
exception obvious and well-scoped. The rest of the latchkey package still
goes through ``ConcurrencyGroup`` for any managed subprocess work.
"""

import os
import subprocess
from pathlib import Path


def spawn_detached_latchkey_gateway(
    latchkey_binary: str,
    listen_host: str,
    listen_port: int,
    log_path: Path,
    latchkey_directory: Path | None = None,
) -> int:
    """Start a detached ``latchkey gateway`` and return its PID.

    The child is placed in its own session (``setsid`` via
    ``start_new_session=True``) so it survives the caller's death. Its
    stdout/stderr are appended to ``log_path`` (the parent directory is
    created if needed). It reads listen host/port from the environment
    variables latchkey documents (``LATCHKEY_GATEWAY_LISTEN_*``).

    When ``latchkey_directory`` is supplied, ``LATCHKEY_DIRECTORY`` is set
    in the child's environment so all minds-managed gateways share a single
    credential / config directory instead of falling back to ``~/.latchkey``.
    The parent directory is created if needed.

    The returned ``Popen`` object is intentionally allowed to go out of
    scope. Python's ``subprocess`` module parks finished children on an
    internal ``_active`` list for zombie reaping, but never kills a
    still-running child during garbage collection, so the gateway keeps
    running until something explicitly terminates it.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["LATCHKEY_GATEWAY_LISTEN_HOST"] = listen_host
    env["LATCHKEY_GATEWAY_LISTEN_PORT"] = str(listen_port)
    if latchkey_directory is not None:
        latchkey_directory.mkdir(parents=True, exist_ok=True)
        env["LATCHKEY_DIRECTORY"] = str(latchkey_directory)

    log_file = log_path.open("ab")
    try:
        process = subprocess.Popen(
            [latchkey_binary, "gateway"],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        # Our copy of the log file descriptor can be closed; the child
        # inherited its own dup via Popen's stdio setup.
        log_file.close()
    return process.pid


def spawn_detached_latchkey_ensure_browser(
    latchkey_binary: str,
    log_path: Path,
    latchkey_directory: Path | None = None,
) -> int:
    """Start a detached ``latchkey ensure-browser`` and return its PID.

    The command discovers and configures a browser for Latchkey to use,
    downloading Chromium via Playwright if no system browser is found.
    This can take a while on first run, so we fire it off detached and let
    it complete (or not) in the background; if minds exits first, the next
    session will re-check.

    Child is placed in its own session via ``start_new_session=True`` so it
    survives the caller's death. Stdout/stderr are appended to ``log_path``
    (the parent directory is created if needed). When ``latchkey_directory``
    is supplied, ``LATCHKEY_DIRECTORY`` is set in the child's environment so
    the browser configuration lands in the shared minds-managed directory
    instead of falling back to ``~/.latchkey``.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if latchkey_directory is not None:
        latchkey_directory.mkdir(parents=True, exist_ok=True)
        env["LATCHKEY_DIRECTORY"] = str(latchkey_directory)

    log_file = log_path.open("ab")
    try:
        process = subprocess.Popen(
            [latchkey_binary, "ensure-browser"],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log_file.close()
    return process.pid
