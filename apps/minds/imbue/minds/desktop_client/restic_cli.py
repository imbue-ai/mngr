"""Run the local ``restic`` binary from the minds app.

minds initializes each workspace's restic repository itself (so the
workspace never needs the master password or any repo-init logic) and
queries repositories for backup status. Both require ``restic`` on the
machine running minds. Repository address + backend credentials are passed
to restic via the environment (``RESTIC_REPOSITORY`` plus e.g. ``AWS_*``);
the password is passed as ``RESTIC_PASSWORD``, or via the global
``--insecure-no-password`` flag when the password is empty.
"""

import json
import os
import shutil
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final
from typing import NoReturn
from typing import TypeVar

from loguru import logger
from tenacity import RetryCallState
from tenacity import Retrying
from tenacity import retry_if_exception_type
from tenacity import stop_after_delay
from tenacity import wait_fixed

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.minds.errors import BackupProvisioningError

_T = TypeVar("_T")


def _get_restic_binary() -> str:
    """Resolve the restic CLI path.

    Prefers ``MINDS_RESTIC_BINARY`` -- the bundled binary that ships in
    ``resources/restic/restic``. Electron's backend.js sets it in both
    dev and packaged mode (via ``paths.getResticPath()``); tests get it
    from the session conftest. End users -- and devs running tests --
    never need a system-wide restic install. Falls back to ``"restic"``
    (PATH lookup) only as a last resort.

    Resolved at every call site rather than at import time so a fixture
    can set the env var before the first restic operation runs.
    """
    return os.environ.get("MINDS_RESTIC_BINARY") or "restic"


# restic treats locks older than 30 minutes as stale and ignores them.
_LOCK_STALE_SECONDS: Final[float] = 30 * 60.0
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 60.0
_INIT_TIMEOUT_SECONDS: Final[float] = 120.0
# Restoring a whole snapshot can take a while for a large workspace.
_RESTORE_TIMEOUT_SECONDS: Final[float] = 600.0
# A freshly-minted R2 / S3 credential can take a few seconds to become active
# at the storage backend's edge; the first restic call against it then fails
# with an auth error ("Unauthorized" / "InvalidAccessKeyId"). Retry the repo
# bootstrap for a bounded window so provisioning rides out that propagation.
_AUTH_PROPAGATION_RETRY_SECONDS: Final[float] = 60.0
_AUTH_PROPAGATION_WAIT_SECONDS: Final[float] = 3.0
_TRANSIENT_AUTH_SIGNALS: Final[tuple[str, ...]] = ("unauthorized", "invalidaccesskeyid", "signaturedoesnotmatch")


class ResticNotInstalledError(BackupProvisioningError):
    """Raised when the ``restic`` binary is not available on the minds machine."""


class ResticTransientAuthError(BackupProvisioningError):
    """Raised when restic auth fails in a way that is likely transient.

    Typically a just-minted storage credential that has not yet propagated to
    the backend edge; a short retry succeeds.
    """


def ensure_restic_available() -> None:
    """Raise ``ResticNotInstalledError`` if ``restic`` cannot be located.

    The bundled binary at ``resources/restic/restic`` is the expected
    source; a missing binary means the dev tree hasn't run ``pnpm
    build`` (which downloads restic) and ``pnpm start``'s ``prestart``
    hook hasn't run either.
    """
    binary = _get_restic_binary()
    if shutil.which(binary) is None:
        raise ResticNotInstalledError(
            f"restic binary not found at {binary!r}. The minds build is supposed "
            "to bundle it; run `pnpm build` in apps/minds/ to download "
            "resources/restic/restic, or set MINDS_RESTIC_BINARY explicitly."
        )


def _run_restic(
    args: Sequence[str],
    *,
    env_overrides: Mapping[str, str],
    parent_cg: ConcurrencyGroup | None,
    timeout_seconds: float,
) -> FinishedProcess:
    """Run ``restic <args...>`` with ``env_overrides`` merged onto the process env."""
    ensure_restic_available()
    env = dict(os.environ)
    env.update(env_overrides)
    cg = parent_cg.make_concurrency_group(name="restic") if parent_cg is not None else ConcurrencyGroup(name="restic")
    with cg:
        return cg.run_process_to_completion(
            command=[_get_restic_binary(), *args],
            env=env,
            timeout=float(timeout_seconds),
            is_checked_after=False,
        )


def _env_and_flags(
    repository: str,
    backend_env: Mapping[str, str],
    password: str | None,
) -> tuple[dict[str, str], list[str]]:
    """Build the restic env overlay + global flags for one repository.

    An empty (or absent) password selects ``--insecure-no-password`` rather
    than setting ``RESTIC_PASSWORD``; restic rejects combining the two.
    """
    env = dict(backend_env)
    env["RESTIC_REPOSITORY"] = repository
    if password:
        env["RESTIC_PASSWORD"] = password
        return env, []
    return env, ["--insecure-no-password"]


def _looks_already_initialized(stderr: str) -> bool:
    """Return whether a ``restic init`` failure means the repo already exists."""
    lowered = stderr.lower()
    return "already initialized" in lowered or "already exists" in lowered


def _looks_like_transient_auth_failure(stderr: str) -> bool:
    """Return whether a restic failure looks like a not-yet-active credential."""
    lowered = stderr.lower()
    return any(signal in lowered for signal in _TRANSIENT_AUTH_SIGNALS)


def _raise_restic_failure(operation_label: str, returncode: int | None, stderr: str) -> NoReturn:
    """Raise the right error for a failed restic invocation.

    Auth failures that look like a freshly-minted credential still propagating
    raise the retryable ``ResticTransientAuthError``; everything else is fatal.
    """
    detail = stderr.strip()
    if _looks_like_transient_auth_failure(stderr):
        raise ResticTransientAuthError(f"{operation_label} auth not ready (exit {returncode}): {detail}")
    raise BackupProvisioningError(f"{operation_label} failed (exit {returncode}): {detail}")


def _log_auth_retry(retry_state: RetryCallState) -> None:
    logger.debug(
        "Retrying restic after transient auth failure (freshly-minted credential likely still propagating); attempt {}",
        retry_state.attempt_number,
    )


def _retry_on_transient_auth(
    operation: Callable[[], _T],
    *,
    timeout_seconds: float = _AUTH_PROPAGATION_RETRY_SECONDS,
    wait_seconds: float = _AUTH_PROPAGATION_WAIT_SECONDS,
) -> _T:
    """Run ``operation``, retrying only ``ResticTransientAuthError`` for a bounded window."""
    retryer = Retrying(
        retry=retry_if_exception_type(ResticTransientAuthError),
        stop=stop_after_delay(timeout_seconds),
        wait=wait_fixed(wait_seconds),
        before_sleep=_log_auth_retry,
        reraise=True,
    )
    return retryer(operation)


def _init_repo_once(
    *,
    repository: str,
    backend_env: Mapping[str, str],
    password: str | None,
    parent_cg: ConcurrencyGroup | None,
) -> None:
    """Run a single ``restic init`` attempt; treat an already-initialized repo as success."""
    env, flags = _env_and_flags(repository, backend_env, password)
    result = _run_restic(
        [*flags, "init"], env_overrides=env, parent_cg=parent_cg, timeout_seconds=_INIT_TIMEOUT_SECONDS
    )
    if result.returncode == 0:
        return
    if _looks_already_initialized(result.stderr):
        logger.debug("restic repo already initialized; reusing it")
        return
    _raise_restic_failure("restic init", result.returncode, result.stderr)


def init_repo(
    *,
    repository: str,
    backend_env: Mapping[str, str],
    password: str | None,
    parent_cg: ConcurrencyGroup | None = None,
) -> None:
    """``restic init`` the repository; treat an already-initialized repo as success.

    Retries a transient auth failure (a freshly-minted storage credential that
    has not yet propagated to the backend edge) for a bounded window.
    """
    _retry_on_transient_auth(
        lambda: _init_repo_once(repository=repository, backend_env=backend_env, password=password, parent_cg=parent_cg)
    )


def _add_password_key_once(
    *,
    repository: str,
    backend_env: Mapping[str, str],
    existing_password: str | None,
    new_password: str,
    parent_cg: ConcurrencyGroup | None,
) -> None:
    """Run a single ``restic key add`` attempt, authenticating with ``existing_password``."""
    env, flags = _env_and_flags(repository, backend_env, existing_password)
    with TemporaryDirectory() as temp_dir:
        new_password_file = Path(temp_dir) / "new_password"
        # 0600 temp file so the random key isn't briefly world-readable on disk.
        fd = os.open(new_password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, new_password.encode("utf-8"))
        finally:
            os.close(fd)
        result = _run_restic(
            [*flags, "key", "add", "--new-password-file", str(new_password_file)],
            env_overrides=env,
            parent_cg=parent_cg,
            timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
        )
    if result.returncode != 0:
        _raise_restic_failure("restic key add", result.returncode, result.stderr)


def add_password_key(
    *,
    repository: str,
    backend_env: Mapping[str, str],
    existing_password: str | None,
    new_password: str,
    parent_cg: ConcurrencyGroup | None = None,
) -> None:
    """Add ``new_password`` as an additional key, authenticating with ``existing_password``.

    Retries a transient auth failure for a bounded window (see ``init_repo``).
    """
    _retry_on_transient_auth(
        lambda: _add_password_key_once(
            repository=repository,
            backend_env=backend_env,
            existing_password=existing_password,
            new_password=new_password,
            parent_cg=parent_cg,
        )
    )


def restore_snapshot(
    *,
    repository: str,
    backend_env: Mapping[str, str],
    password: str | None,
    target_dir: Path,
    snapshot: str = "latest",
    parent_cg: ConcurrencyGroup | None = None,
    timeout_seconds: float = _RESTORE_TIMEOUT_SECONDS,
) -> None:
    """Restore ``snapshot`` into ``target_dir`` via ``restic restore``.

    ``restic restore`` downloads blobs in parallel, so it is dramatically faster
    than ``restic dump`` (which fetches sequentially) for a many-file snapshot.
    """
    env, flags = _env_and_flags(repository, backend_env, password)
    result = _run_restic(
        [*flags, "restore", snapshot, "--target", str(target_dir)],
        env_overrides=env,
        parent_cg=parent_cg,
        timeout_seconds=timeout_seconds,
    )
    if result.returncode != 0:
        raise BackupProvisioningError(f"restic restore failed (exit {result.returncode}): {result.stderr.strip()}")


def parse_restic_timestamp(raw: str) -> datetime | None:
    """Parse a restic RFC3339 timestamp (which may carry nanoseconds) to UTC.

    ``datetime.fromisoformat`` only accepts up to microseconds, so any
    sub-microsecond fractional digits are trimmed first. Returns None if the
    value can't be parsed.
    """
    text = raw.strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    # Trim a fractional-seconds component to at most 6 digits.
    if "." in normalized:
        head, _, tail = normalized.partition(".")
        digits = ""
        rest = ""
        for index, char in enumerate(tail):
            if char.isdigit():
                digits += char
            else:
                rest = tail[index:]
                break
        normalized = f"{head}.{digits[:6]}{rest}"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def get_latest_snapshot_time(
    *,
    repository: str,
    backend_env: Mapping[str, str],
    password: str | None,
    parent_cg: ConcurrencyGroup | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> datetime | None:
    """Return the time of the most recent snapshot, or None if there are none."""
    env, flags = _env_and_flags(repository, backend_env, password)
    result = _run_restic(
        [*flags, "--no-lock", "snapshots", "--latest", "1", "--json"],
        env_overrides=env,
        parent_cg=parent_cg,
        timeout_seconds=timeout_seconds,
    )
    if result.returncode != 0:
        raise BackupProvisioningError(f"restic snapshots failed (exit {result.returncode}): {result.stderr.strip()}")
    try:
        snapshots = json.loads(result.stdout or "[]")
    except ValueError as e:
        raise BackupProvisioningError(f"restic snapshots returned non-JSON output: {e}") from e
    times = [
        parse_restic_timestamp(str(snapshot["time"]))
        for snapshot in snapshots
        if isinstance(snapshot, dict) and snapshot.get("time")
    ]
    real_times = [time for time in times if time is not None]
    return max(real_times) if real_times else None


def is_backup_in_progress(
    *,
    repository: str,
    backend_env: Mapping[str, str],
    password: str | None,
    now: datetime,
    parent_cg: ConcurrencyGroup | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> bool:
    """Return whether the repository currently has a non-stale lock.

    A non-stale lock (younger than restic's ~30-minute staleness window)
    means some restic operation -- in practice the workspace's hourly backup
    -- is running. Stale locks (left by a killed process) are ignored.
    """
    env, flags = _env_and_flags(repository, backend_env, password)
    listed = _run_restic(
        [*flags, "--no-lock", "list", "locks"],
        env_overrides=env,
        parent_cg=parent_cg,
        timeout_seconds=timeout_seconds,
    )
    if listed.returncode != 0:
        raise BackupProvisioningError(f"restic list locks failed (exit {listed.returncode}): {listed.stderr.strip()}")
    lock_ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    for lock_id in lock_ids:
        shown = _run_restic(
            [*flags, "--no-lock", "cat", "lock", lock_id],
            env_overrides=env,
            parent_cg=parent_cg,
            timeout_seconds=timeout_seconds,
        )
        if shown.returncode != 0:
            # The lock vanished between listing and reading it (a backup just
            # finished); ignore it rather than failing the whole status check.
            continue
        try:
            lock = json.loads(shown.stdout)
        except ValueError:
            continue
        lock_time = parse_restic_timestamp(str(lock.get("time", "")))
        if lock_time is not None and (now - lock_time).total_seconds() < _LOCK_STALE_SECONDS:
            return True
    return False
