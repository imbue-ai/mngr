"""Private helpers: spawn detached ``latchkey`` / ``mngr latchkey forward`` subprocesses.

Why this file uses raw ``subprocess.Popen`` (triggering a narrow exclusion
from ``check_direct_subprocess``): we need the spawned processes to *outlive*
the caller. ``latchkey ensure-browser`` may download Chromium via
Playwright, which can take a while; detaching means we do not block
desktop-client shutdown on it and the next minds session will simply
re-check. ``mngr latchkey forward`` is the long-running supervisor that
owns the shared gateway and per-agent reverse tunnels; detaching means a
minds restart adopts the existing instance rather than tearing down and
re-establishing every tunnel.

Confining the remaining ``Popen`` calls to this tiny helper keeps the
ratchet exception obvious and well-scoped. The rest of the latchkey
package still goes through ``ConcurrencyGroup`` for any managed
subprocess work.
"""

import os
import subprocess
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final
from typing import IO

from loguru import logger
from pydantic import SecretStr

from imbue.imbue_common.logging import generate_rotation_timestamp
from imbue.mngr_latchkey.encryption_key import inject_encryption_key_into_env
from imbue.mngr_latchkey.store import forward_events_log_path
from imbue.mngr_latchkey.store import plugin_data_dir as _plugin_data_dir

# Size cap for a raw stdout/stderr capture file, and how many rotations to keep.
# These files stay near-empty in steady state (the children are spawned
# ``--quiet``, so all real logging goes to the structured JSONL), but a capture
# written by an older build -- or a child that dies in a crash loop before its
# logging is configured -- can leave a large file behind. It is append-only and
# its fd is handed to a detached child, so nothing else ever truncates it; left
# alone it is also gzipped and re-uploaded with every bug report.
_MAX_RAW_CAPTURE_BYTES: Final[int] = 10 * 1024 * 1024
# One rotation, not a history: with the children spawned ``--quiet`` these files
# do not grow in steady state, so this fires at most once on a given install --
# on one carrying a large capture from an older build. Keeping more copies would
# just retain more of the same stale bulk on disk.
_MAX_RAW_CAPTURE_ROTATIONS: Final[int] = 1


def _rotate_raw_capture_if_too_large(log_path: Path) -> None:
    """Rotate an oversized raw capture file before its fd is handed to a child.

    Spawn time is the only safe moment to rotate: the file's descriptor is
    passed straight to a detached child, so once that child is running a rename
    would leave it writing to the old inode. Here no child holds the fd yet.

    Oldest rotations beyond :data:`_MAX_RAW_CAPTURE_ROTATIONS` are removed.
    Rotation failures are deliberately not fatal -- failing to spawn the
    supervisor because a log file could not be renamed would trade a cosmetic
    problem for a broken app -- but they are logged, since a rotation that keeps
    failing (permissions, a full disk) is otherwise invisible.
    """
    try:
        if not log_path.exists() or log_path.stat().st_size <= _MAX_RAW_CAPTURE_BYTES:
            return
        log_path.rename(log_path.with_name(f"{log_path.name}.{generate_rotation_timestamp()}"))
        rotations = sorted(log_path.parent.glob(f"{log_path.name}.*"), key=lambda path: path.name)
        for stale in rotations[:-_MAX_RAW_CAPTURE_ROTATIONS]:
            stale.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("Could not rotate the oversized raw capture log at {}: {}", log_path, e)


def _write_spawn_marker(log_file: IO[bytes], command_description: str) -> None:
    """Append a timestamped spawn marker to a raw capture file.

    The child's own output cannot be stamped from here: its stdout/stderr fd is
    handed over directly (a pipe would break the detachment this spawn exists to
    provide), so the parent never sees the individual lines. The marker instead
    dates the boundary, which is what makes the file readable -- anything below
    it was emitted by the run that started at this time, so a crash traceback
    can be placed against the other (timestamped) logs.

    As with rotation, a failure here is deliberately not fatal -- a full disk
    makes the marker unwritable (``open`` succeeds; the buffered write only
    fails at ``flush``), and refusing to spawn the supervisor over an unwritable
    log line would trade a cosmetic problem for a broken app -- but it is logged.
    """
    marker = f"{datetime.now(timezone.utc).isoformat()} === spawning {command_description} ===\n"
    try:
        log_file.write(marker.encode())
        log_file.flush()
    except OSError as e:
        logger.warning("Could not write the spawn marker to the raw capture log at {}: {}", log_file.name, e)


def spawn_detached_latchkey_ensure_browser(
    latchkey_binary: str,
    log_path: Path,
    latchkey_directory: Path | None = None,
    encryption_key: SecretStr | None = None,
) -> int:
    """Start a detached ``latchkey ensure-browser`` and return its PID.

    The command discovers and configures a browser for Latchkey to use,
    downloading Chromium via Playwright if no system browser is found.
    This can take a while on first run, so we fire it off detached and let
    it complete (or not) in the background; if minds exits first, the next
    session will re-check.

    Child is placed in its own session via ``start_new_session=True`` so it
    survives the caller's death. Stdout/stderr are appended to ``log_path``
    (the parent directory is created if needed), below a timestamped marker
    naming this run; since the child holds the fd directly, spawn time is also
    when an oversized capture is rotated. When ``latchkey_directory``
    is supplied, ``LATCHKEY_DIRECTORY`` is set in the child's environment so
    the browser configuration lands in the shared minds-managed directory
    instead of falling back to ``~/.latchkey``.

    ``encryption_key`` is injected as ``LATCHKEY_ENCRYPTION_KEY`` so the
    detached child never falls through to Latchkey's startup key-resolution
    against the system keychain. Latchkey resolves (and migrates) its
    encryption key on every non-gateway CLI invocation -- including
    ``ensure-browser``, which does not otherwise touch credentials -- so
    without this var set, macOS would pop a keychain access dialog on every
    spawn.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if latchkey_directory is not None:
        latchkey_directory.mkdir(parents=True, exist_ok=True)
        env["LATCHKEY_DIRECTORY"] = str(latchkey_directory)
    inject_encryption_key_into_env(env, encryption_key)

    _rotate_raw_capture_if_too_large(log_path)
    log_file = log_path.open("ab")
    try:
        _write_spawn_marker(log_file, "latchkey ensure-browser")
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


def spawn_detached_mngr_latchkey_forward(
    mngr_binary: str,
    latchkey_binary: str,
    latchkey_directory: Path,
    log_path: Path,
    extra_env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> int:
    """Start a detached ``mngr latchkey forward`` and return its PID.

    The child is placed in its own session (``setsid`` via
    ``start_new_session=True``) so it survives the caller's death. Its
    stdout/stderr are appended to ``log_path`` (the parent directory is
    created if needed). ``--latchkey-directory`` and ``--latchkey-binary``
    are passed explicitly so the subprocess uses the exact same latchkey
    state the caller knows about, regardless of any env / settings.toml
    state the inherited environment carries.

    ``cwd`` sets the spawned process's working directory. Callers that want
    the supervisor to behave like a laptop-side ``mngr`` invocation (the minds
    desktop client) pass ``$HOME`` so it does not resolve project config from a
    transient cwd -- e.g. a dev checkout whose ``.mngr/settings.toml`` would
    otherwise be loaded (and, under a pytest run, rejected by mngr's config
    guard). ``None`` inherits the caller's cwd.

    ``extra_env`` lets callers add (or override) environment variables
    for the spawned process; these flow into the ``latchkey gateway``
    subprocess via :func:`imbue.mngr_latchkey.core._build_gateway_env`
    (which inherits ``os.environ``) and from there into any gateway
    extension's ``process.env``. Used by the minds desktop client to
    publish the current ``LATCHKEY_EXTENSION_MINDS_API_URL`` to the
    bundled ``minds-api-proxy`` extension on every supervisor restart.

    The returned ``Popen`` object is intentionally allowed to go out of
    scope. Python's ``subprocess`` module parks finished children on an
    internal ``_active`` list for zombie reaping, but never kills a
    still-running child during garbage collection, so the forward
    supervisor keeps running until something explicitly terminates it.

    Two logs are produced. The process is pointed via ``--log-file`` at a
    co-located structured JSONL log (:func:`forward_events_log_path`), which
    carries nanosecond timestamps and is rotated mid-run by the standard mngr
    file sink -- this is the log to read when you need to observe timing.
    ``log_path`` is the raw stdout/stderr capture; its fd is handed straight to
    the child so it cannot be rotated mid-write -- it is instead rotated here,
    at spawn time, while no child holds the fd. We pass ``--quiet`` so the
    child suppresses its loguru console handler (everything still goes to the
    structured log), which keeps this raw file from accumulating in steady
    state -- it then only ever captures rare startup-failure output (Click
    errors, pre-logging tracebacks) that never reaches the structured log,
    below a timestamped marker naming the run that produced it.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    latchkey_directory.mkdir(parents=True, exist_ok=True)

    events_log_path = forward_events_log_path(_plugin_data_dir(latchkey_directory))

    env = dict(os.environ)
    if extra_env is not None:
        env.update(extra_env)

    _rotate_raw_capture_if_too_large(log_path)
    log_file = log_path.open("ab")
    try:
        _write_spawn_marker(log_file, "mngr latchkey forward")
        process = subprocess.Popen(
            [
                mngr_binary,
                "latchkey",
                "forward",
                "--latchkey-directory",
                str(latchkey_directory),
                "--latchkey-binary",
                latchkey_binary,
                "--mngr-binary",
                mngr_binary,
                "--log-file",
                str(events_log_path),
                # Suppress the detached child's loguru console handler so its
                # raw stdout/stderr capture file stays empty in steady state;
                # all logging still lands in the structured ``--log-file``.
                "--quiet",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            env=env,
            cwd=str(cwd) if cwd is not None else None,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log_file.close()
    return process.pid
