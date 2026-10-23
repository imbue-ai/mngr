"""Rewind a workspace's whole backup root to one restic snapshot, in place.

Gate on chats/ticks, stop every supervisord service (they all run from and
write into the tree this restore rewrites), take a ``pre-restore`` safety
snapshot (so any restore is undoable), then
``restic restore <id>:<subpath> --target <backup_root> --delete`` with
``--overwrite if-changed``: no staging copy, only changed files are
rewritten, and a failed restore converges when simply re-run. It excludes
``.ssh/authorized_keys`` and the downloaded fallback restic, so ``--delete``
leaves them in place though the snapshot lacks them. The current
``restic.env`` is written back afterwards, a ``restored`` snapshot of the
restored state is appended (tagged with the source snapshot's time, so the
timeline shows the restored version as a new "Restored from ..." entry), then
``uv sync --all-packages`` and a ``supervisorctl reload``, which brings the
services back from the restored tree's supervisord config as a boot would
(``restart all`` instead when that config does not parse). Every exit path
after the service stop brings the services back the same way (best-effort).

The backup root is /home/user on the current layout (the host dir itself on
legacy /mngr workspaces). Parameterized via argv: --agent-id, --snapshot-id,
--snapshot-subpath (the directory inside the snapshot that corresponds to the
backup root, resolved by Imbue Studio from its own view of the repository) and
--source-time, plus the optional flags --stop-chats, --skip-chat-gate (an
explicit user "force restore" on a workspace that can no longer answer
`mngr list`) and --skip-safety-snapshot (an explicit user "restore without
backing up first" after the safety snapshot failed). The program never queries
restic for metadata Imbue Studio already holds. Verdict: ok | blocked (running
chats) | failed, on one ``RESTORE_RESULT_MARKER`` line.

The in-place restore needs restic >= 0.17; when the workspace's restic is
older, the program downloads the pinned, sha256-verified build and installs it
persistently (shadowing the distro binary, so the whole workspace converges on
the pinned version). Long restic operations stream throttled progress lines on
stdout so the desktop can show a live accounting.
"""

import bz2
import hashlib
import json
import os
import platform
import select
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Final
from typing import NoReturn

from imbue.minds.workspace_programs.backup_protocol import RESTORE_RESULT_MARKER
from imbue.minds.workspace_programs.backup_protocol import STATUS_FAILED
from imbue.minds.workspace_programs.backup_protocol import STATUS_OK
from imbue.minds.workspace_programs.backup_support import RESTIC_ENV_PATH
from imbue.minds.workspace_programs.backup_support import SERVICE_VERIFY_TIMEOUT_SECONDS
from imbue.minds.workspace_programs.backup_support import SHORT_COMMAND_TIMEOUT_SECONDS
from imbue.minds.workspace_programs.backup_support import gate_chats_and_wait_for_tick
from imbue.minds.workspace_programs.backup_support import output_tail
from imbue.minds.workspace_programs.program_support import argv_value
from imbue.minds.workspace_programs.program_support import as_json_object
from imbue.minds.workspace_programs.program_support import emit_marker_json
from imbue.minds.workspace_programs.program_support import has_argv_flag
from imbue.minds.workspace_programs.program_support import poll_until
from imbue.minds.workspace_programs.program_support import run_command
from imbue.minds.workspace_programs.program_support import write_progress_line

_RESTIC_TIMEOUT_SECONDS: Final[float] = 3000.0
_RESTIC_UNLOCK_TIMEOUT_SECONDS: Final[float] = 120.0
_RESTIC_VERSION_TIMEOUT_SECONDS: Final[float] = 30.0
_RESTIC_WAIT_AFTER_OUTPUT_CLOSED_SECONDS: Final[float] = 60.0
_RESTIC_READ_POLL_SECONDS: Final[float] = 5.0
_RESTIC_OUTPUT_TAIL_LINE_COUNT: Final[int] = 40
_ALL_SERVICES_COMMAND_TIMEOUT_SECONDS: Final[float] = 300.0
_UV_SYNC_TIMEOUT_SECONDS: Final[float] = 900.0
# The in-place restore needs `restic restore --delete` / `--overwrite`,
# which landed in restic 0.17. When the machine's restic is older (Debian
# bookworm ships 0.14), the pinned build below is downloaded, sha256-verified
# and installed persistently.
_MINIMUM_RESTIC_VERSION: Final[tuple[int, int, int]] = (0, 17, 0)
_PINNED_RESTIC_VERSION: Final[str] = "0.18.1"
_PINNED_RESTIC_SHA256_BY_ARCH: Final[dict[str, str]] = {
    "amd64": "680838f19d67151adba227e1570cdd8af12c19cf1735783ed1ba928bc41f363d",
    "arm64": "87f53fddde38764095e9c058a3b31834052c37e5826d2acf34e18923c006bd45",
}
_RESTIC_ARCH_BY_MACHINE: Final[dict[str, str]] = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}
_RESTIC_DOWNLOAD_TIMEOUT_SECONDS: Final[float] = 300.0
# Where the downloaded restic lands when /usr/local/bin is not writable. Kept
# out of snapshots (excluded below): it is a regenerable 25MB binary.
_FALLBACK_RESTIC_DIR_NAME: Final[str] = ".minds-restic"
_FALLBACK_RESTIC_EXCLUDE: Final[str] = "**/" + _FALLBACK_RESTIC_DIR_NAME
# host_backup's marker for a directory an app can rebuild: the snapshots keep
# only the marker file, as the hourly backups do.
_NO_BACKUP_MARKER_FILENAME: Final[str] = ".nobackup"
# Forward at most one restic --json status line per interval: restic emits
# them far faster than a human (or the SSE log stream) needs.
_PROGRESS_INTERVAL_SECONDS: Final[float] = 2.0
# Fallback excludes for the safety/restored snapshots when the machine has
# no readable backup.toml excludes. Matches host_backup's built-in
# defaults so those snapshots look like every hourly snapshot; when the user
# customized excludes in backup.toml, theirs are used instead (read below).
_DEFAULT_SNAPSHOT_EXCLUDES: Final[tuple[str, ...]] = (
    "**/.venv",
    "**/node_modules",
    "**/__pycache__",
    "**/.pytest_cache",
    "**/.ruff_cache",
    "**/target",
    "**/dist",
    "**/build",
    "**/.next",
    "**/.cache",
    "**/.cargo/registry",
    "**/.cargo/git",
    "**/.rustup/toolchains",
    "**/.rustup/downloads",
    "**/.ssh/authorized_keys",
    "**/data/.state/update-apply/snapshots",
    "**/data/.state/isolated-instances/*/copies",
    "**/data/.state/isolated-instances/*/scratch",
)
# Paths the in-place restore never touches, whatever the snapshot holds.
# Snapshots never carry authorized_keys, so a restore would delete it; on
# remote workspaces it holds Imbue Studio's key for the container's sshd (root's home
# is the backup root). Nor do they reliably carry the fallback restic, which
# this restore is running and still needs for the restored-state snapshot.
_RESTORE_PRESERVED_PATHS: Final[tuple[str, ...]] = ("**/.ssh/authorized_keys", _FALLBACK_RESTIC_EXCLUDE)

# Service-health model for the post-restore verification. A service is
# "healthy" when RUNNING, or when it ran to completion (supervisord reports
# one-shots like env-converge as EXITED). The verdict gates on the
# restore-critical set and on nothing else (the contract is
# behaviors/backup-restore/restore-verdict.feature): a restored workspace
# must come back able to serve its user, and every other service's recovery
# is reported, never fatal -- the environment converges stragglers in the
# background, and a service that stays down needs attention independent of
# this restore. The backup service is deliberately OUTSIDE the set: its
# health has its own verification and repair flows, and the pre-restore
# safety snapshot exists regardless.
# A program configured autostart=false is not started by a boot or a reload,
# so it is not judged at all: it is the user's or an agent's to start.
_HEALTHY_SERVICE_STATES: Final[tuple[str, ...]] = ("RUNNING", "EXITED")
_RESTORE_CRITICAL_SERVICES: Final[tuple[str, ...]] = ("system_interface",)
_SERVICE_SETTLE_POLL_SECONDS: Final[float] = 2.0

# Cleanup obligations of the current run. Every side effect the restore takes
# registers its cleanup here, and _finish pays the debts (best-effort) before
# reporting -- so no individual failure path can forget to bring the services
# back. `is_resume_owed` flips on right after `stop all` and off once the
# success path has brought the services back and is verifying them.
_DEBTS: Final[dict[str, bool]] = {"is_resume_owed": False}


def _all_service_states() -> dict[str, str]:
    # name -> supervisord state for every configured program. supervisorctl's
    # exit code is non-zero whenever any program is not RUNNING, so only the
    # output matters here. {} when supervisorctl is unusable, which reads as
    # every restore-critical service absent -- a failure, since a machine
    # that cannot answer for its core cannot be verified alive.
    status = run_command(["supervisorctl", "status"], SHORT_COMMAND_TIMEOUT_SECONDS)
    states: dict[str, str] = {}
    for line in (status.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            states[parts[0]] = parts[1]
    return states


def _healthy_service_names(states: Mapping[str, str]) -> set[str]:
    return set(name for name, state in states.items() if state in _HEALTHY_SERVICE_STATES)


def _manual_programs() -> set[str]:
    # Names of the programs configured autostart=false, which `supervisorctl
    # avail` marks "manual" in its next-to-last column. Empty when supervisorctl
    # is unusable, so every program is judged.
    avail = run_command(["supervisorctl", "avail"], SHORT_COMMAND_TIMEOUT_SECONDS)
    manual: set[str] = set()
    for line in (avail.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[-2] == "manual":
            manual.add(parts[0])
    return manual


def _unhealthy_services(
    states: Mapping[str, str], services_up_before: set[str], manual_programs: set[str]
) -> tuple[list[str], list[str]]:
    # (critical_down, other_down): sorted names not healthy in `states`.
    # Critical is absolute -- absent from the roster counts as down, and so
    # does a critical service that was already down. Every other name is
    # judged against `services_up_before`, making other_down a difference
    # rather than an absolute: what this restore took down and did not bring
    # back. A program configured autostart=false is never judged: the reload
    # leaves it stopped as a boot would, even one started by hand before the
    # restore.
    critical_down = sorted(
        name for name in _RESTORE_CRITICAL_SERVICES if states.get(name) not in _HEALTHY_SERVICE_STATES
    )
    other_down = sorted(
        name
        for name, state in states.items()
        if state not in _HEALTHY_SERVICE_STATES
        and name not in _RESTORE_CRITICAL_SERVICES
        and name in services_up_before
        and name not in manual_programs
    )
    return critical_down, other_down


def _sample_service_health(services_up_before: set[str]) -> tuple[dict[str, str], list[str], list[str]]:
    states = _all_service_states()
    critical_down, other_down = _unhealthy_services(states, services_up_before, _manual_programs())
    return states, critical_down, other_down


def _wait_for_service_recovery(
    timeout_seconds: float, services_up_before: set[str]
) -> tuple[dict[str, str], list[str], list[str]]:
    # Poll until one status sample shows every counted service healthy, or
    # the deadline passes; returns the final (states, critical_down,
    # other_down). Waiting on more than the restore-critical set gives
    # stragglers their settle time before the success warning names them,
    # while the all-healthy common case exits on the first sample. Requiring
    # a single all-healthy sample (rather than first-sight-of-RUNNING per
    # service) also catches a service that spawns and promptly crash-loops.
    return poll_until(
        lambda: _sample_service_health(services_up_before),
        lambda health: not health[1] and not health[2],
        timeout_seconds,
        _SERVICE_SETTLE_POLL_SECONDS,
    )


def _resume_services(result: dict[str, object]) -> tuple[str, subprocess.CompletedProcess[str]]:
    # `reload` brings the services back as a boot would, from the config the
    # tree now holds: programs the restored tree lacks are dropped, ones it adds
    # are loaded, and only autostart=true ones start. `restart all` would start
    # every program loaded before the restore, autostart=false ones included.
    # A `reload` of a config supervisord cannot parse takes supervisord down
    # with every service, so `reread` checks it first.
    reread = run_command(["supervisorctl", "reread"], SHORT_COMMAND_TIMEOUT_SECONDS)
    if reread.returncode == 0:
        command = "reload"
    else:
        command = "restart all"
        result["supervisord_config_error"] = ((reread.stdout or "") + (reread.stderr or "")).strip()[-500:]
    resumed = run_command(["supervisorctl", *command.split()], _ALL_SERVICES_COMMAND_TIMEOUT_SECONDS)
    result["services_resumed_by"] = command
    result["services_restarted"] = resumed.returncode == 0
    return command, resumed


def _finish(result: dict[str, object], status: str, detail: str) -> NoReturn:
    if _DEBTS["is_resume_owed"]:
        _resume_services(result)
        _DEBTS["is_resume_owed"] = False
    result["status"] = status
    if detail:
        result["detail"] = detail
    emit_marker_json(RESTORE_RESULT_MARKER, result)
    sys.exit(0)


def _parse_env_lines(content: str) -> dict[str, str]:
    env_map: dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        env_map[key.strip()] = value.strip()
    return env_map


def _restic_environment(env_map: Mapping[str, str]) -> dict[str, str]:
    env = dict(os.environ)
    env.update(env_map)
    return env


def _restic_version_tuple(binary: str) -> tuple[int, int, int] | None:
    # The (major, minor, patch) of `binary`, or None when it is unusable.
    result = run_command([binary, "version"], _RESTIC_VERSION_TIMEOUT_SECONDS)
    if result.returncode != 0:
        return None
    for token in (result.stdout or "").split():
        parts = token.split(".")
        if len(parts) < 2 or not all(part.isdigit() for part in parts):
            continue
        padded = parts + ["0", "0"]
        return (int(padded[0]), int(padded[1]), int(padded[2]))
    return None


def _install_restic_binary(binary_bytes: bytes, destination: str) -> bool:
    # Written via a temp file + rename so a concurrent reader never sees a
    # half-written binary.
    try:
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(destination))
        with os.fdopen(fd, "wb") as fh:
            fh.write(binary_bytes)
        os.chmod(temp_path, 0o755)
        os.replace(temp_path, destination)
    except OSError:
        return False
    return True


def _download_pinned_restic(fallback_path: str) -> tuple[str, str]:
    # Download + verify the pinned restic; returns (installed_path, error).
    machine = platform.machine().lower()
    arch = _RESTIC_ARCH_BY_MACHINE.get(machine)
    if arch is None:
        return "", "no pinned restic build for architecture %s" % machine
    url = "https://github.com/restic/restic/releases/download/v%s/restic_%s_linux_%s.bz2" % (
        _PINNED_RESTIC_VERSION,
        _PINNED_RESTIC_VERSION,
        arch,
    )
    write_progress_line("Downloading restic %s (%s)..." % (_PINNED_RESTIC_VERSION, arch))
    try:
        with urllib.request.urlopen(url, timeout=_RESTIC_DOWNLOAD_TIMEOUT_SECONDS) as response:
            compressed: bytes = response.read()
    except OSError as e:
        return "", "could not download restic from %s: %s" % (url, e)
    if hashlib.sha256(compressed).hexdigest() != _PINNED_RESTIC_SHA256_BY_ARCH[arch]:
        return "", "the downloaded restic does not match its pinned sha256; refusing to install it"
    try:
        binary_bytes = bz2.decompress(compressed)
    except (OSError, ValueError) as e:
        return "", "could not decompress the restic download: %s" % e
    # Persist: prefer /usr/local/bin (shadows the distro binary on PATH, so
    # the whole machine -- including the hourly host-backup service --
    # converges on the pinned version); fall back to a host-dir location when
    # that is not writable.
    for destination in ("/usr/local/bin/restic", fallback_path):
        if not _install_restic_binary(binary_bytes, destination):
            continue
        version = _restic_version_tuple(destination)
        if version is not None and version >= _MINIMUM_RESTIC_VERSION:
            write_progress_line("Installed restic %s at %s." % (_PINNED_RESTIC_VERSION, destination))
            return destination, ""
    return "", "could not install the downloaded restic binary"


def _resolve_restic_binary(backup_root: str, result: dict[str, object]) -> tuple[str, str]:
    # Returns (binary, error): any restic at/above the minimum, downloading
    # the pinned one if needed.
    fallback_path = os.path.join(backup_root, _FALLBACK_RESTIC_DIR_NAME, "restic")
    for candidate in ("restic", fallback_path):
        version = _restic_version_tuple(candidate)
        if version is not None and version >= _MINIMUM_RESTIC_VERSION:
            return candidate, ""
    installed, error = _download_pinned_restic(fallback_path)
    if installed:
        result["restic_downloaded"] = True
    return installed, error


def _read_snapshot_excludes(code_dir: str) -> list[str]:
    # The user's current backup.toml excludes (or host_backup's defaults when
    # absent/unreadable) plus its extra_excludes. The file lives at
    # data/system/backup.toml on the decluttered template layout and
    # runtime/backup.toml on pre-declutter machines; read whichever exists so
    # the safety/restored snapshots honor the same excludes as the machine's
    # own hourly snapshots.
    toml_path = os.path.join(code_dir, "data", "system", "backup.toml")
    if not os.path.isfile(toml_path):
        toml_path = os.path.join(code_dir, "runtime", "backup.toml")
    excludes = list(_DEFAULT_SNAPSHOT_EXCLUDES)
    if os.path.isfile(toml_path):
        try:
            with open(toml_path, "rb") as fh:
                raw: dict[str, object] = tomllib.load(fh)
        except (OSError, ValueError):
            raw = {}
        configured = raw.get("excludes")
        if isinstance(configured, list) and configured and all(isinstance(p, str) for p in configured):
            excludes = [str(p) for p in configured]
        extra = raw.get("extra_excludes")
        if isinstance(extra, list) and all(isinstance(p, str) for p in extra):
            excludes += [str(p) for p in extra]
    # The downloaded restic fallback binary is regenerable; never snapshot it.
    return excludes + [_FALLBACK_RESTIC_EXCLUDE]


def _snapshot_backup_args(backup_root: str, tags: Sequence[str], excludes: Sequence[str]) -> list[str]:
    # `restic backup` args for the safety/restored snapshots, leaving out what
    # the hourly backups leave out.
    args = ["backup", backup_root]
    for tag in tags:
        args += ["--tag", tag]
    args += ["--exclude-if-present", _NO_BACKUP_MARKER_FILENAME]
    for pattern in excludes:
        args += ["--exclude", pattern]
    return args


def _human_bytes(count: float) -> str:
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0:
            return "%.1f %s" % (value, unit)
        value /= 1024.0
    return "%.1f TB" % value


def _format_restic_progress(line: str) -> str | None:
    # Translates one restic --json output line into a progress line, or None
    # to drop it.
    if not line.startswith("{"):
        return line[:300]
    try:
        payload = as_json_object(json.loads(line))
    except ValueError:
        return line[:300]
    if payload is None:
        return None
    message_type = payload.get("message_type")
    if message_type == "status":
        percent = payload.get("percent_done")
        if not isinstance(percent, (int, float)):
            return None
        done = payload.get("bytes_done", payload.get("bytes_restored"))
        total = payload.get("total_bytes")
        detail = ""
        if isinstance(done, (int, float)) and isinstance(total, (int, float)) and total:
            detail = " (%s / %s)" % (_human_bytes(done), _human_bytes(total))
        return "progress: %d%%%s" % (int(percent * 100), detail)
    if message_type == "summary":
        return "progress: 100%"
    if message_type == "error":
        return "restic error: %s" % json.dumps(payload)[:300]
    return None


def _handle_restic_output_line(raw_line: str, tail: list[str], progress_state: dict[str, float]) -> None:
    # Keeps the line in the output tail and forwards it as progress, at most
    # one percentage line per interval.
    stripped = raw_line.strip()
    if not stripped:
        return
    tail.append(stripped)
    del tail[:-_RESTIC_OUTPUT_TAIL_LINE_COUNT]
    message = _format_restic_progress(stripped)
    if message is None:
        return
    if message.startswith("progress:"):
        now = time.monotonic()
        if now - progress_state["last_forwarded_at"] < _PROGRESS_INTERVAL_SECONDS:
            return
        progress_state["last_forwarded_at"] = now
    write_progress_line(message)


def _restic_streaming(
    args: Sequence[str], env_map: Mapping[str, str], restic_binary: str, timeout_seconds: float
) -> tuple[int, str]:
    # Runs one restic command, streaming throttled progress; returns
    # (returncode, output_tail).
    try:
        process = subprocess.Popen(
            [restic_binary, *args, "--json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=_restic_environment(env_map),
        )
    except OSError as e:
        return 127, "failed to run %s: %s" % (restic_binary, e)
    assert process.stdout is not None
    deadline = time.monotonic() + timeout_seconds
    tail: list[str] = []
    progress_state = {"last_forwarded_at": 0.0}
    fd = process.stdout.fileno()
    buffered = b""

    # Loop until restic closes its output (break) or the deadline passes (the
    # loop condition fails and the while's else reports the timeout).
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        ready, _, _ = select.select([fd], [], [], max(min(remaining, _RESTIC_READ_POLL_SECONDS), 0.0))
        if not ready:
            continue
        chunk = os.read(fd, 65536)
        if chunk == b"":
            break
        buffered += chunk
        while b"\n" in buffered:
            raw, buffered = buffered.split(b"\n", 1)
            _handle_restic_output_line(raw.decode("utf-8", "replace"), tail, progress_state)
    else:
        process.kill()
        process.wait()
        return 124, "restic %s timed out after %d seconds" % (args[0], int(timeout_seconds))
    if buffered:
        _handle_restic_output_line(buffered.decode("utf-8", "replace"), tail, progress_state)
    try:
        returncode = process.wait(timeout=_RESTIC_WAIT_AFTER_OUTPUT_CLOSED_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        returncode = process.wait()
    return returncode, "\n".join(tail)


def _is_repository_lock_failure(returncode: int, output: str) -> bool:
    # restic >= 0.17 exits 11 on a locked repository; the message match covers
    # subcommand/version variance.
    return returncode == 11 or "repository is already locked" in output or "unable to create lock" in output


def _restic_step_with_unlock_retry(
    args: Sequence[str], env_map: Mapping[str, str], restic_binary: str
) -> tuple[int, str]:
    # Runs a restic step, clearing a stale repository lock and retrying once
    # if needed. A tick killed by this restore's own `stop all` leaves a lock
    # owned by a dead process; `restic unlock` removes exactly such locks (it
    # never touches a live holder's), so one unlock+retry converges -- the
    # same pattern the host_backup service uses.
    returncode, output = _restic_streaming(args, env_map, restic_binary, _RESTIC_TIMEOUT_SECONDS)
    if returncode == 0 or not _is_repository_lock_failure(returncode, output):
        return returncode, output
    write_progress_line(
        "The repository is locked (likely a backup this restore interrupted); clearing the stale lock..."
    )
    run_command([restic_binary, "unlock"], _RESTIC_UNLOCK_TIMEOUT_SECONDS, env=_restic_environment(env_map))
    return _restic_streaming(args, env_map, restic_binary, _RESTIC_TIMEOUT_SECONDS)


def _resolve_backup_root() -> str:
    # The restore target is the BACKUP ROOT -- the whole persistent tree the
    # host-backup service snapshots. Current layout: /home/user, of which
    # MNGR_HOST_DIR is the hidden .mngr child; legacy machines backed up the
    # host dir itself, so there the target stays MNGR_HOST_DIR.
    mngr_host_dir = os.path.realpath(os.environ.get("MNGR_HOST_DIR", "/home/user/.mngr"))
    if os.path.basename(mngr_host_dir) == ".mngr":
        return os.path.dirname(mngr_host_dir)
    return mngr_host_dir


def _service_verify_timeout_seconds() -> float:
    # Test hook: production dispatch never passes this; unit tests shrink the
    # post-restart settle wait so failure paths stay fast.
    try:
        return float(argv_value("--service-verify-timeout-seconds", str(SERVICE_VERIFY_TIMEOUT_SECONDS)))
    except ValueError:
        return SERVICE_VERIFY_TIMEOUT_SECONDS


def _restore_snapshot_in_place(
    result: dict[str, object],
    snapshot_id: str,
    snapshot_subpath: str,
    backup_root: str,
    env_map: Mapping[str, str],
    restic_binary: str,
) -> None:
    """Rewrite the backup root to match the snapshot, failing the run when that does not complete.

    The in-place sync restore: only files that differ from the snapshot are
    rewritten, files the snapshot lacks are deleted, and nothing is staged --
    so no double disk, and a restore that fails midway converges when simply
    re-run. The subpath maps the snapshot's layout onto the backup root;
    Imbue Studio resolved and validated it before dispatch.
    """
    write_progress_line("Restoring the selected backup into place...")
    # The restore may rewrite or delete this process's original cwd entries.
    os.chdir("/")
    restore_args = [
        "restore",
        "%s:%s" % (snapshot_id, snapshot_subpath),
        "--target",
        backup_root,
        "--delete",
        "--overwrite",
        "if-changed",
    ]
    # restic's --delete only removes paths the restore filter selects, so an
    # excluded path is left as it is -- provided the snapshot has its parent
    # directory, since a directory the snapshot lacks is deleted whole.
    for pattern in _RESTORE_PRESERVED_PATHS:
        restore_args += ["--exclude", pattern]
    restored, restore_output = _restic_step_with_unlock_retry(restore_args, env_map, restic_binary)
    if restored != 0:
        detail = (
            "the in-place restore failed (%s); the workspace may be mixed between versions -- "
            "running the restore again picks up where this one stopped." % restore_output[-500:]
        )
        _finish(result, STATUS_FAILED, detail)
    # Sanity-check the restored tree by its repo checkout: workspace/ in the
    # current layout, code/ in legacy snapshots.
    if not os.path.isdir(os.path.join(backup_root, "workspace")) and not os.path.isdir(
        os.path.join(backup_root, "code")
    ):
        _finish(
            result, STATUS_FAILED, "the restore completed but left no workspace/ or code/ checkout at %s" % backup_root
        )
    result["restored"] = True


def _bring_services_back_and_verify(
    result: dict[str, object], services_up_before: set[str], service_verify_timeout: float
) -> NoReturn:
    """Resume every service from the restored tree's config and judge the restore by the ones that came back.

    This pays the resume debt directly (and clears it first, so a failed
    resume is not blindly retried by _finish) because the success path must
    also verify the services came back. The re-run env-converge one-shot
    converges the (untouched) rootfs back to the restored environment record
    -- that is what makes a restore reproduce the restored package set, not
    just files. The resume's exit code is recorded but deliberately NOT the
    success gate: `restart all` goes non-zero when ANY program fails to start
    immediately, and only the restore-critical set decides the verdict.
    """
    write_progress_line("Restarting the workspace services...")
    _DEBTS["is_resume_owed"] = False
    resume_command, resumed = _resume_services(result)
    post_states, critical_down, other_down = _wait_for_service_recovery(service_verify_timeout, services_up_before)
    if critical_down:
        resume_tail = (resumed.stderr or resumed.stdout).strip()[-300:]
        detail = "restored, but the restore-critical service(s) did not come back: %s%s" % (
            ", ".join("%s (%s)" % (name, post_states.get(name) or "absent") for name in critical_down),
            ("; %s said: %s" % (resume_command, resume_tail)) if resumed.returncode != 0 else "",
        )
        _finish(result, STATUS_FAILED, detail)
    if other_down:
        result["services_down"] = other_down
    _finish(result, STATUS_OK, "")


def main() -> None:
    agent_id = argv_value("--agent-id", "")
    snapshot_id = argv_value("--snapshot-id", "")
    # Resolved by Imbue Studio from its own view of the repository and passed in, so
    # this program never queries restic for snapshot metadata.
    snapshot_subpath = argv_value("--snapshot-subpath", "")
    source_time = argv_value("--source-time", "")
    is_stop_chats = has_argv_flag("--stop-chats")
    is_chat_gate_skipped = has_argv_flag("--skip-chat-gate")
    is_safety_snapshot_skipped = has_argv_flag("--skip-safety-snapshot")
    service_verify_timeout = _service_verify_timeout_seconds()
    result: dict[str, object] = {
        "schema": 2,
        "safety_snapshot_taken": False,
        "safety_snapshot_skipped": is_safety_snapshot_skipped,
        "restored": False,
        "services_restarted": False,
        "restored_snapshot_taken": False,
        "restic_downloaded": False,
    }
    if not snapshot_id:
        _finish(result, STATUS_FAILED, "no --snapshot-id provided")
    if not snapshot_subpath:
        _finish(result, STATUS_FAILED, "no --snapshot-subpath provided")

    # Gate before anything mutates. The chat half needs the current machine
    # code (`uv run mngr`); a forced restore skips only that half -- the tick
    # wait is self-healing (a stopped host-backup service means no live tick)
    # and needs no machine code.
    if is_chat_gate_skipped:
        write_progress_line("Skipping the running-chats check (forced restore).")
    gate_status, gate_extra, gate_detail = gate_chats_and_wait_for_tick(
        agent_id, is_stop_chats, is_chat_gate_skipped=is_chat_gate_skipped
    )
    result.update(gate_extra)
    if gate_status != STATUS_OK:
        _finish(result, gate_status, gate_detail)

    # Read the current credentials, which the restore must keep working.
    code_dir = os.path.realpath(os.getcwd())
    backup_root = _resolve_backup_root()
    env_path = os.path.join(code_dir, RESTIC_ENV_PATH)
    try:
        with open(env_path, "rb") as env_file:
            env_content = env_file.read()
    except OSError as e:
        _finish(result, STATUS_FAILED, "cannot read %s: %s" % (RESTIC_ENV_PATH, e))
    env_map = _parse_env_lines(env_content.decode("utf-8", "replace"))
    if not env_map.get("RESTIC_REPOSITORY"):
        _finish(result, STATUS_FAILED, "%s has no RESTIC_REPOSITORY" % RESTIC_ENV_PATH)

    # Resolve a usable restic (downloading the pinned build if the installed
    # one is too old) and the snapshot excludes before anything is stopped or
    # mutated, so these failures are cheap and leave the machine untouched.
    restic_binary, restic_error = _resolve_restic_binary(backup_root, result)
    if not restic_binary:
        _finish(result, STATUS_FAILED, restic_error)
    excludes = _read_snapshot_excludes(code_dir)

    # Read before anything is stopped: the verdict is relative to this.
    services_up_before = _healthy_service_names(_all_service_states())

    # Quiesce the machine: every supervisord service runs from (and writes
    # into) the backup root this restore rewrites, so a service left running
    # could recreate or hold files mid-restore. Stop them all -- the exec
    # channel this program runs through is not supervisord-managed, so it
    # survives. Best-effort: already-stopped (or missing) services must not
    # abort the restore. From here on, every exit owes a resume -- registered
    # as a debt that _finish itself pays, so no failure path can forget it.
    write_progress_line("Stopping the workspace services...")
    run_command(["supervisorctl", "stop", "all"], _ALL_SERVICES_COMMAND_TIMEOUT_SECONDS)
    _DEBTS["is_resume_owed"] = True

    # Safety snapshot of the current state, so this restore is itself
    # undoable. Skipped only on an explicit user re-dispatch after this very
    # step failed.
    if not is_safety_snapshot_skipped:
        write_progress_line("Backing up the current state (safety snapshot)...")
        backup_args = _snapshot_backup_args(backup_root, ["pre-restore"], excludes)
        backed_up, backup_output = _restic_step_with_unlock_retry(backup_args, env_map, restic_binary)
        if backed_up != 0:
            _finish(result, STATUS_FAILED, "pre-restore safety snapshot failed: %s" % backup_output[-500:])
        result["safety_snapshot_taken"] = True

    _restore_snapshot_in_place(result, snapshot_id, snapshot_subpath, backup_root, env_map, restic_binary)

    # The snapshot carries whatever restic.env it had at backup time (possibly
    # none); the current credentials must keep working, so write them back.
    try:
        os.makedirs(os.path.dirname(env_path), exist_ok=True)
        with open(env_path, "wb") as env_file:
            env_file.write(env_content)
    except OSError as e:
        _finish(result, STATUS_FAILED, "restored, but could not re-write %s: %s" % (RESTIC_ENV_PATH, e))

    # Append a snapshot of the restored state, tagged with its lineage, so the
    # backup timeline shows the restored version as a new entry ("Restored
    # from <source time>") sitting above the pre-restore safety backup. Nearly
    # free: restic dedups against the source snapshot's blobs, so only new
    # metadata is written. Taken now, while the services are still stopped, so
    # nothing is writing to the tree. Best-effort: the restore itself already
    # succeeded and the next backup tick would capture this state anyway, so a
    # failure here must not fail the operation.
    write_progress_line("Recording the restored state in the backup timeline...")
    restored_tags = ["restored"]
    if source_time:
        restored_tags.append("restored-from:%s" % source_time)
    restored_backup_args = _snapshot_backup_args(backup_root, restored_tags, excludes)
    restored_snapshot, _ = _restic_step_with_unlock_retry(restored_backup_args, env_map, restic_binary)
    result["restored_snapshot_taken"] = restored_snapshot == 0

    # Backups exclude regenerable trees (.venv etc.), so rebuild dependencies
    # before the services come back. --all-packages matters: a root-closure
    # sync would prune workspace members that are not root dependencies from
    # the venv, deleting their console scripts and spawn-erroring their `uv
    # run <name>` supervisord programs on the restart below.
    write_progress_line("Reinstalling dependencies (uv sync)...")
    synced = run_command(["uv", "sync", "--all-packages"], _UV_SYNC_TIMEOUT_SECONDS, cwd=code_dir)
    if synced.returncode != 0:
        _finish(result, STATUS_FAILED, "restored, but uv sync failed: %s" % output_tail(synced, 800))

    _bring_services_back_and_verify(result, services_up_before, service_verify_timeout)


# No catch-all here: an unexpected crash prints its traceback to stderr and
# produces no marker, which the desktop side treats as "program died" -- it
# surfaces the stderr tail and dispatches its own best-effort resume command
# (the same backstop that covers an exec timeout, which no in-program handler
# could survive anyway).
if __name__ == "__main__":
    main()
