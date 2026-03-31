"""Shared pytest conftest hooks for all projects in the monorepo.

Provides common test infrastructure:
- Global test locking (prevents parallel pytest processes from conflicting)
- Test suite timing limits (configurable via PYTEST_MAX_DURATION_SECONDS env var)
- xdist parallelism override (configurable via PYTEST_NUMPROCESSES env var)
- Output file redirection (slow tests report, coverage report)
- Shared pytest defaults (markers, filterwarnings, CLI args, coverage report config)
- Cached importlib.metadata.entry_points() for fast test startup on slow filesystems
- Resource mark enforcement (ensures tests are correctly marked for external tool usage)
- Test profiles: branch-name-based selective testing (see test_profiles.toml)

Environment variables:
- PYTEST_NUMPROCESSES: Override the number of xdist workers (default: 4, set in
  pyproject.toml addopts). Set to e.g. 16 on machines with many cores, or 0 to
  disable xdist. This overrides the -n value from pyproject.toml but NOT an
  explicit -n passed on the command line.
- PYTEST_MAX_DURATION_SECONDS: Override the maximum allowed test suite duration in seconds.
  Without this, defaults are chosen based on test type and environment (see
  _compute_max_duration for details).
- MNGR_TEST_PROFILE: Force a specific test profile (overrides branch detection).
  Set to "all" to disable profile filtering entirely.

Usage in each project's conftest.py:
    from imbue.imbue_common.conftest_hooks import register_conftest_hooks
    register_conftest_hooks(globals())

The register_conftest_hooks function uses a module-level guard to ensure hooks
are only registered once. This is critical because when running from the monorepo
root, both the root conftest.py AND per-project conftest.py files are discovered
by pytest. Without the guard, pytest_addoption would fail with duplicate option errors.
"""

import fcntl
import importlib.metadata
import json
import os
import signal
import sys
import time
from io import StringIO
from pathlib import Path
from typing import Any
from typing import Final
from typing import TextIO
from uuid import uuid4

import pytest
from coverage.exceptions import CoverageException

from imbue.imbue_common.test_profiles import ScopedProfile
from imbue.imbue_common.test_profiles import resolve_active_profile
from imbue.resource_guards.resource_guards import start_resource_guards
from imbue.resource_guards.resource_guards import stop_resource_guards

# ---------------------------------------------------------------------------
# Cache importlib.metadata.entry_points() to avoid repeated filesystem scans.
#
# On slow filesystems (e.g., 9p with dcache=0), each entry_points() call takes
# ~50-90ms because it must stat/read dist-info directories for every installed
# package. With ~3000 tests that each trigger entry_points() via plugin loading
# and connector discovery, this adds up to minutes of pure I/O overhead.
#
# Since installed packages don't change during a test run, we cache results at
# module import time. Each xdist worker is a separate process, so a simple
# in-process dict is sufficient (no cross-process coordination needed).
# ---------------------------------------------------------------------------

_original_entry_points = importlib.metadata.entry_points
_entry_points_cache: dict[
    tuple[tuple[str, Any], ...],
    Any,
] = {}


def _cached_entry_points(
    **params: Any,
) -> Any:
    """Caching wrapper around importlib.metadata.entry_points().

    Converts the keyword arguments to a hashable key (frozenset of items) and
    returns a cached result if available. Entry points are static for the
    lifetime of a test process, so the cache never needs invalidation.
    """
    key = tuple(sorted(params.items()))
    if key not in _entry_points_cache:
        _entry_points_cache[key] = _original_entry_points(**params)
    return _entry_points_cache[key]


importlib.metadata.entry_points = _cached_entry_points  # type: ignore[assignment]


# Directory for test output files (slow tests, coverage summaries).
# Relative to wherever pytest is invoked from.
_TEST_OUTPUTS_DIR: Final[Path] = Path(".test_output")

# The lock file path - a constant location in /tmp so all pytest processes can find it
_GLOBAL_TEST_LOCK_PATH: Final[Path] = Path("/tmp/pytest_global_test_lock")

# Attribute name used to store the lock file handle on the session object.
# The handle must stay open for the duration of the test session so the flock is held.
# When the process exits for any reason, the OS closes the handle and releases the lock.
_SESSION_LOCK_HANDLE_ATTR: Final[str] = "_global_test_lock_file_handle"

# Grace period added to the max duration when computing the lock deadline.
# This accounts for test collection, teardown, and other overhead beyond
# the raw test execution time that _compute_max_duration() measures.
_LOCK_DEADLINE_GRACE_SECONDS: Final[float] = 60.0

# Guard to prevent duplicate hook registration (see module docstring).
_registered: bool = False


# ---------------------------------------------------------------------------
# Shared defaults -- the single source of truth for markers, filterwarnings,
# and coverage report settings that are common across all projects.
# Per-project pyproject.toml files still contain addopts (CLI args like -n,
# --timeout, --cov, etc.) and coverage.run settings (parallel, concurrency,
# omit) because those must be parsed before hooks run.
# ---------------------------------------------------------------------------

_SHARED_MARKERS: Final[list[str]] = [
    "acceptance: marks tests as requiring network access, Modal credentials, etc. These are required to pass in CI",
    "release: marks tests as being required for release (but not for merging PRs)",
]

# Additional markers registered by projects via register_marker().
_registered_markers: list[str] = []


def register_marker(marker_line: str) -> None:
    """Register a pytest marker to be added during pytest_configure.

    Call this from each project's conftest.py before register_conftest_hooks().
    The marker_line format is "name: description" (same as pyproject.toml markers).
    """
    if marker_line not in _registered_markers:
        _registered_markers.append(marker_line)


_SHARED_FILTER_WARNINGS: Final[list[str]] = [
    # Suppress grpclib warning that occurs during garbage collection when Channel.__del__ is called
    # after the connection's transport has already been cleaned up. This is a known issue in grpclib.
    "ignore:Exception ignored in.*Channel.__del__.*:pytest.PytestUnraisableExceptionWarning",
    # Suppress coverage warning about modules being imported before coverage starts measuring.
    # This happens because pytest collects tests (importing modules) before coverage.py starts.
    r"ignore:Module imbue\..* was previously imported, but not measured:coverage.exceptions.CoverageWarning",
    # record_xml_attribute is marked experimental but we rely on it for JUnit test ID customization.
    "ignore::pytest.PytestExperimentalApiWarning",
]

# Lines matching any of these patterns are excluded from coverage measurement.
_SHARED_COVERAGE_EXCLUDE_LINES: Final[list[str]] = [
    "pragma: no cover",
    "def __repr__",
    "case _ as unreachable:",
    "assert_never(unreachable)",
    "raise AssertionError",
    "raise NotImplementedError",
    "if __name__ == .__main__.:",
    "if TYPE_CHECKING:",
    "@abstractmethod",
    r"^\s*\.\.\.$",  # Matches lines containing only "..."
]


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _is_xdist_worker() -> bool:
    """Return True if we are running as an xdist worker process."""
    return "PYTEST_XDIST_WORKER" in os.environ


def _print_lock_message(message: str, fd: int = 2) -> None:
    """Print a message that will show even without pytest's -s flag.

    Writes directly to the file descriptor (default: 2 = stderr) to bypass
    any output capturing that pytest or xdist may be doing.
    """
    os.write(fd, f"\n{message}\n".encode())


def _is_process_alive(pid: int) -> bool:
    """Check whether a process with the given PID is still running.

    Also reaps the process if it is a zombie child of the current process,
    since zombies respond to ``os.kill(pid, 0)`` even though they have exited.
    """
    # Reap if it is a zombie child of ours.  For non-children this raises
    # ChildProcessError which we silently ignore.
    try:
        waited_pid, _ = os.waitpid(pid, os.WNOHANG)
        if waited_pid == pid:
            return False
    except ChildProcessError:
        pass

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by a different user.
        return True


def _kill_stale_process(pid: int) -> bool:
    """Send SIGTERM then SIGKILL to a process that has exceeded its deadline.

    Returns True if the signals were delivered (the process should be dying or
    already dead). Returns False if the process could not be signalled (e.g. due
    to insufficient permissions).
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False

    # Give the process a moment to shut down gracefully before escalating.
    # Human-sanctioned use of time.sleep -- there is no event-based mechanism
    # to wait for an arbitrary (non-child) process to exit.
    time.sleep(5)

    if not _is_process_alive(pid):
        return True

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return True


def _read_lock_info(lock_path: Path) -> dict[str, Any] | None:
    """Read and parse the JSON lock info from the lock file.

    Returns None if the file is missing, empty, contains invalid JSON, or
    the top-level value is not a dict.
    """
    try:
        content = lock_path.read_text()
        if not content.strip():
            return None
        parsed = json.loads(content)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def _write_lock_info(lock_handle: TextIO, pid: int, deadline: float | None) -> None:
    """Write JSON lock info (pid and optional deadline) to the lock file handle."""
    lock_info: dict[str, int | float] = {"pid": pid}
    if deadline is not None:
        lock_info["deadline"] = deadline
    lock_handle.write(json.dumps(lock_info))
    lock_handle.flush()


def _compute_max_duration() -> float:
    """Compute the maximum allowed test suite duration in seconds.

    The same logic is used by _pytest_sessionfinish to enforce the time limit
    and by _compute_lock_deadline to derive the lock file deadline.

    There are 4 types of tests, each with different time limits in CI:

    - unit tests: fast, local, no network (run together with integration tests)
    - integration tests: local, no network, used for coverage calculation
    - acceptance tests: run on all branches except release, have network/Modal/etc access
    - release tests: only run on release, comprehensive tests for release readiness
    """
    if "PYTEST_MAX_DURATION_SECONDS" in os.environ:
        return float(os.environ["PYTEST_MAX_DURATION_SECONDS"])
    # Release tests have the highest limit since there can be many, and they can be slow
    if os.environ.get("IS_RELEASE", "0") == "1":
        return 10 * 60.0
    # Acceptance tests have a somewhat higher limit than integration/unit
    if os.environ.get("IS_ACCEPTANCE", "0") == "1":
        return 6 * 60.0
    if "CI" in os.environ:
        # Integration + unit tests in CI should be fast
        return 150.0
    # Local development default
    return 300.0


def _compute_lock_deadline(start_time: float) -> float | None:
    """Compute the lock deadline as an absolute timestamp.

    Returns a deadline only when PYTEST_MAX_DURATION_SECONDS is explicitly set, indicating
    that the caller is aware of a time budget (e.g. invoked from a hook or script).
    When no explicit budget is set, returns None so that other processes will not
    kill this one (though they can still clean up a dead PID).
    """
    if "PYTEST_MAX_DURATION_SECONDS" not in os.environ:
        return None
    max_duration = _compute_max_duration()
    return start_time + max_duration + _LOCK_DEADLINE_GRACE_SECONDS


def _try_break_stale_lock(lock_path: Path) -> bool:
    """Attempt to break a stale or expired lock.

    Reads the lock file to check the owning PID and optional deadline:
    - If the PID is no longer alive, removes the lock file and returns True.
    - If the PID is alive but its deadline has passed, kills it, removes the
      lock file, and returns True.
    - Otherwise returns False (lock is legitimately held).
    """
    lock_info = _read_lock_info(lock_path)
    if lock_info is None:
        return False

    pid = lock_info.get("pid")
    if not isinstance(pid, int):
        return False

    if not _is_process_alive(pid):
        _print_lock_message(
            f"PYTEST GLOBAL LOCK: Removing stale lock from dead process (pid={pid}).",
        )
        lock_path.unlink(missing_ok=True)
        return True

    deadline = lock_info.get("deadline")
    if isinstance(deadline, (int, float)) and time.time() > deadline:
        overdue = time.time() - deadline
        _print_lock_message(
            f"PYTEST GLOBAL LOCK: Process {pid} exceeded its deadline (by {overdue:.0f}s), sending SIGTERM+SIGKILL.",
        )
        if _kill_stale_process(pid):
            lock_path.unlink(missing_ok=True)
            return True
        _print_lock_message(
            f"PYTEST GLOBAL LOCK: Could not kill process {pid}, will wait for lock.",
        )

    return False


def _verify_lock_inode(lock_handle: TextIO, lock_path: Path) -> bool:
    """Check that the open file handle still refers to the same on-disk inode.

    After acquiring an flock, the file may have been unlinked and recreated by
    another process that broke a stale lock.  In that case our flock is on a
    deleted inode and does not provide mutual exclusion with the new file.
    """
    fd_stat = os.fstat(lock_handle.fileno())
    try:
        path_stat = lock_path.stat()
    except FileNotFoundError:
        return False
    return fd_stat.st_ino == path_stat.st_ino


def _acquire_global_test_lock(
    lock_path: Path,
) -> TextIO:
    """Acquire an exclusive lock on the given path, returning the open file handle.

    If the lock cannot be acquired immediately, the lock file is inspected for a
    JSON payload containing the holder's PID and an optional deadline:

    - If the holder's PID is no longer running, the stale lock file is removed
      and acquisition is retried immediately.
    - If the holder is still alive but its deadline has passed, the holder is
      killed (SIGTERM then SIGKILL), the lock file is removed, and acquisition
      is retried.
    - Otherwise, blocks until the lock becomes available.

    The caller must keep the returned file handle open for as long as they want
    to hold the lock. The lock is automatically released when the file handle is
    closed or when the process exits.
    """
    acquired_handle: TextIO | None = None
    waited_for_lock = False
    while acquired_handle is None:
        # Ensure the lock file exists.
        lock_path.touch(exist_ok=True)

        try:
            lock_file_handle = lock_path.open("r+")
        except FileNotFoundError:
            # Narrow race: file was deleted between touch() and open().
            continue

        # Try to acquire the lock without blocking.
        try:
            fcntl.flock(lock_file_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Lock is held by another process.
            lock_file_handle.close()

            if _try_break_stale_lock(lock_path):
                continue

            # Legitimate lock holder -- block until it finishes.
            _print_lock_message(
                "PYTEST GLOBAL LOCK: Another pytest process is running.\n"
                "Waiting for it to complete before starting this test run...",
            )
            lock_path.touch(exist_ok=True)
            try:
                lock_file_handle = lock_path.open("r+")
            except FileNotFoundError:
                continue
            fcntl.flock(lock_file_handle.fileno(), fcntl.LOCK_EX)
            waited_for_lock = True

        # Verify the inode has not been replaced while we waited.
        if _verify_lock_inode(lock_file_handle, lock_path):
            # We hold the real lock.  Truncate so the caller can write fresh info.
            lock_file_handle.seek(0)
            lock_file_handle.truncate()
            acquired_handle = lock_file_handle
        else:
            lock_file_handle.close()

    if waited_for_lock:
        _print_lock_message("PYTEST GLOBAL LOCK: Lock acquired, proceeding with tests.")

    return acquired_handle


def _configure_shared_coverage_defaults(config: pytest.Config) -> None:
    """Apply shared coverage report settings to the Coverage object.

    Configures:
    - report exclude_lines (code patterns to exclude from coverage)
    - report formatting (skip_empty, precision, sort)
    - html output directory

    These settings are shared across all projects in the monorepo so they
    don't need to be duplicated in each pyproject.toml.

    Note: coverage.run settings (parallel, concurrency, omit) must remain in
    each pyproject.toml because they are read at Coverage.__init__ time, before
    any hooks run.

    Must be called after pytest-cov has created the Coverage object (i.e., after
    pytest_sessionstart). Safe to call when coverage is disabled (--no-cov).
    """
    cov_plugin = config.pluginmanager.get_plugin("_cov")
    if cov_plugin is None:
        return

    cov_controller = getattr(cov_plugin, "cov_controller", None)
    if cov_controller is None:
        return

    cov = getattr(cov_controller, "cov", None)
    if cov is None:
        return

    # Replace the default exclude list with our shared list
    cov.config.exclude_list = list(_SHARED_COVERAGE_EXCLUDE_LINES)

    # Report formatting
    cov.config.skip_empty = True
    cov.config.precision = 2
    cov.config.sort = "Cover"

    # HTML output directory
    cov.config.html_dir = "htmlcov"


# ---------------------------------------------------------------------------
# Pytest hook implementations (prefixed with _ to avoid accidental discovery)
# ---------------------------------------------------------------------------


@pytest.hookimpl(tryfirst=True)
def _pytest_sessionstart(session: pytest.Session) -> None:
    """Acquire the global test lock, record the start time, and finalize coverage suppression.

    Coverage suppression is done here because pytest-cov's CovController is created
    in pytest_configure, but conftest.py's pytest_configure runs after installed plugins.
    By the time our pytest_configure runs, pytest-cov has already copied the cov_report options.
    We modify the CovController here to ensure the terminal report is suppressed.

    The lock prevents multiple parallel pytest processes (e.g., from different worktrees)
    from running tests concurrently, which can cause timing-related flaky tests.

    The lock is acquired at session start (before collection) because with xdist,
    the controller process doesn't run pytest_collection_finish - only workers do.
    By acquiring the lock here, we ensure the controller holds it for the entire session.

    xdist workers skip lock acquisition since the controller already holds it.

    IMPORTANT: The start_time is set AFTER the lock is acquired so that time spent
    waiting for the lock is not counted against the test suite time limit.
    """
    # Suppress coverage terminal output if --coverage-to-file is enabled
    # This needs to be done here because pytest-cov's CovController is created
    # in pytest_configure, after conftest.py's hooks run
    coverage_to_file = getattr(session.config, "_coverage_to_file", False)
    if coverage_to_file:
        cov_plugin = session.config.pluginmanager.get_plugin("_cov")
        if cov_plugin is not None:
            cov_controller = getattr(cov_plugin, "cov_controller", None)
            if cov_controller is not None:
                controller_cov_report = getattr(cov_controller, "cov_report", None)
                if controller_cov_report is not None and isinstance(controller_cov_report, dict):
                    controller_cov_report.pop("term-missing", None)
                    controller_cov_report.pop("term", None)

    # xdist workers should not acquire the lock - only the controller does
    if _is_xdist_worker():
        setattr(session, "start_time", time.time())  # noqa: B010
    else:
        # Acquire the lock and store the handle on the session to keep it open
        lock_handle = _acquire_global_test_lock(lock_path=_GLOBAL_TEST_LOCK_PATH)
        setattr(session, _SESSION_LOCK_HANDLE_ATTR, lock_handle)  # noqa: B010

        # Record start time AFTER acquiring the lock so wait time isn't counted
        start_time = time.time()
        setattr(session, "start_time", start_time)  # noqa: B010

        # Write lock info so other processes can detect stale/expired locks
        deadline = _compute_lock_deadline(start_time)
        _write_lock_info(lock_handle, os.getpid(), deadline)

    start_resource_guards(session)


@pytest.hookimpl(trylast=True)
def _pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Check that the total test session time is under the configured limit.

    Prints per-test durations before checking the limit so that timing data
    is always visible in CI output, even when the suite exceeds the limit.
    """
    stop_resource_guards()

    # Print test durations before checking the time limit, so they are
    # visible in the CI output even when pytest.exit() aborts the session.
    terminalreporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if terminalreporter is not None:
        _print_test_durations_for_ci(terminalreporter)

    if hasattr(session, "start_time"):
        duration = time.time() - session.start_time
        max_duration = _compute_max_duration()

        if duration > max_duration:
            pytest.exit(
                f"Test suite took {duration:.2f}s, exceeding the {max_duration}s limit",
                returncode=1,
            )


def _pytest_addoption(parser: pytest.Parser) -> None:
    """Add options for redirecting slow tests and coverage output to files."""
    group = parser.getgroup("output-to-file", "Options for redirecting output to files")
    group.addoption(
        "--slow-tests-to-file",
        action="store_true",
        default=False,
        help="Write slow tests report to a file instead of stdout",
    )
    group.addoption(
        "--coverage-to-file",
        action="store_true",
        default=False,
        help="Write coverage summary to a file instead of stdout",
    )


def _ensure_test_outputs_dir() -> Path:
    """Ensure the test outputs directory exists and return its path."""
    _TEST_OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    return _TEST_OUTPUTS_DIR


def _generate_output_filename(prefix: str, extension: str) -> Path:
    """Generate a unique filename for test output."""
    return _ensure_test_outputs_dir() / f"{prefix}_{uuid4().hex}{extension}"


@pytest.hookimpl(tryfirst=True)
def _pytest_configure(config: pytest.Config) -> None:
    """Register shared markers/filterwarnings and handle output-to-file options."""
    # Register shared markers and any additional markers from register_marker()
    for marker in _SHARED_MARKERS + _registered_markers:
        config.addinivalue_line("markers", marker)

    # Register shared filterwarnings
    for warning_filter in _SHARED_FILTER_WARNINGS:
        config.addinivalue_line("filterwarnings", warning_filter)

    # Store the slow-tests-to-file option on config for use in hooks
    # Use setattr to avoid type errors - pytest Config doesn't declare these private attributes
    slow_tests_to_file = config.getoption("--slow-tests-to-file", default=False)
    coverage_to_file = config.getoption("--coverage-to-file", default=False)
    setattr(config, "_slow_tests_to_file", slow_tests_to_file)  # noqa: B010
    setattr(config, "_coverage_to_file", coverage_to_file)  # noqa: B010

    # Save the original durations count for our custom reporting, then suppress terminal output
    if slow_tests_to_file:
        original_durations = config.getoption("durations", default=0)
        setattr(config, "_original_durations", original_durations)  # noqa: B010
        # Set durations to None to suppress pytest's built-in terminal output
        # Note: durations=0 shows ALL durations, durations=None suppresses the output
        config.option.durations = None

    # Suppress coverage terminal output when redirecting to file
    if coverage_to_file:
        # Remove term-missing from cov_report options to suppress terminal output
        # but keep html and xml reports
        cov_report = getattr(config.option, "cov_report", None)
        if cov_report is not None and isinstance(cov_report, dict):
            cov_report.pop("term-missing", None)
            cov_report.pop("term", None)

        # Also modify pytest-cov's internal CovController if it exists
        # (it may have already copied the options)
        cov_plugin = config.pluginmanager.get_plugin("_cov")
        if cov_plugin is not None:
            cov_controller = getattr(cov_plugin, "cov_controller", None)
            if cov_controller is not None:
                controller_cov_report = getattr(cov_controller, "cov_report", None)
                if controller_cov_report is not None and isinstance(controller_cov_report, dict):
                    controller_cov_report.pop("term-missing", None)
                    controller_cov_report.pop("term", None)

    # Override xdist worker count from PYTEST_NUMPROCESSES env var.
    # pyproject.toml sets -n 4 as the default (which is needed to activate xdist's
    # DSession plugin during its pytest_configure, which runs before conftest hooks).
    # This override lets different environments (local, CI, Modal) use different
    # parallelism without changing pyproject.toml or passing -n on every invocation.
    # An explicit -n on the command line takes priority over the env var.
    numprocesses_env = os.environ.get("PYTEST_NUMPROCESSES")
    if numprocesses_env is not None:
        cli_has_n_flag = any(arg == "-n" or arg.startswith("-n") for arg in sys.argv[1:])
        if not cli_has_n_flag:
            n = int(numprocesses_env)
            config.option.numprocesses = n
            if n > 0:
                config.option.tx = ["popen"] * n
            else:
                config.option.tx = []
                config.option.dist = "no"

    # Apply test profile filtering based on branch name (see test_profiles.toml).
    # When a profile is active, only tests from its testpaths are collected, and
    # coverage is limited to its cov_packages. Coverage threshold is disabled
    # because thresholds only make sense when all tests run.
    profile = resolve_active_profile(config.rootpath)
    if profile is not None:
        setattr(config, "_test_profile", profile)  # noqa: B010

        # Override coverage sources to only measure profiled packages
        if hasattr(config.option, "cov_source"):
            config.option.cov_source = list(profile.cov_packages)

        # Disable coverage threshold (subset coverage is not meaningful)
        if hasattr(config.option, "cov_fail_under"):
            config.option.cov_fail_under = 0

        # Also update the CovPlugin's options directly. pytest-cov's
        # pytest_configure runs before conftest hooks and stores its own
        # reference to the options Namespace, which may be a different object
        # from config.option.
        cov_plugin = config.pluginmanager.get_plugin("_cov")
        if cov_plugin is not None and hasattr(cov_plugin, "options"):
            cov_plugin.options.cov_source = list(profile.cov_packages)
            cov_plugin.options.cov_fail_under = 0

        if not _is_xdist_worker():
            _print_lock_message(f"TEST PROFILE '{profile.name}' active: testing {', '.join(profile.testpaths)}")


@pytest.hookimpl(tryfirst=True)
def _pytest_collection_modifyitems(
    session: pytest.Session,
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Filter collected test items to only include those matching the active test profile.

    When a test profile is active (set during pytest_configure), items whose file paths
    do not fall under any of the profile's testpaths are deselected. This runs with
    tryfirst=True so that downstream hooks (e.g. pytest-split) only see the filtered set.
    """
    profile: ScopedProfile | None = getattr(config, "_test_profile", None)
    if profile is None:
        return

    allowed_roots = [config.rootpath / tp for tp in profile.testpaths]
    selected: list[pytest.Item] = []
    deselected: list[pytest.Item] = []

    for item in items:
        item_path = item.path
        if any(item_path.is_relative_to(root) for root in allowed_roots):
            selected.append(item)
        else:
            deselected.append(item)

    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected


def _pytest_collection_finish(session: pytest.Session) -> None:
    """Configure shared coverage report settings after test collection.

    This hook runs after pytest_sessionstart (where pytest-cov creates the Coverage
    object), so the coverage config can be safely modified here. Report settings like
    exclude_lines, skip_empty, precision, and sort are only needed at report generation
    time, not during measurement.

    Only runs on the controller process (not xdist workers).
    """
    if _is_xdist_worker():
        return
    _configure_shared_coverage_defaults(session.config)


@pytest.hookimpl(trylast=True)
def _pytest_terminal_summary(
    terminalreporter: "pytest.TerminalReporter",
    exitstatus: int,
    config: pytest.Config,
) -> None:
    """Handle end-of-session output: slow tests file, coverage file, and CI duration printing."""
    # Only run on the controller process (not xdist workers)
    if _is_xdist_worker():
        return

    slow_tests_to_file = getattr(config, "_slow_tests_to_file", False)
    coverage_to_file = getattr(config, "_coverage_to_file", False)

    # Handle slow tests output
    if slow_tests_to_file:
        _write_slow_tests_to_file(terminalreporter, config)

    # Handle coverage output
    if coverage_to_file:
        _write_coverage_summary_to_file(terminalreporter, config)

    # Print all test durations in CI for visibility into per-split timing
    _print_test_durations_for_ci(terminalreporter)


def _collect_test_durations(
    terminalreporter: "pytest.TerminalReporter",
) -> dict[str, float]:
    """Collect test durations from the terminal reporter's stats.

    Returns a dict mapping test node IDs to their call-phase durations.
    Works with xdist because the controller aggregates results from workers.
    """
    durations: dict[str, float] = {}
    for reports in terminalreporter.stats.values():
        for report in reports:
            if hasattr(report, "duration") and hasattr(report, "nodeid"):
                if getattr(report, "when", None) == "call":
                    durations[report.nodeid] = report.duration
    return durations


def _write_slow_tests_to_file(
    terminalreporter: "pytest.TerminalReporter",
    config: pytest.Config,
) -> None:
    """Write the slow tests report to a file."""
    all_durations = _collect_test_durations(terminalreporter)

    # Sort by duration (slowest first)
    durations = sorted(all_durations.items(), key=lambda x: x[1], reverse=True)

    # Get the original durations count (saved before we suppressed terminal output)
    durations_count = getattr(config, "_original_durations", 0)
    if durations_count and durations_count > 0:
        durations = durations[:durations_count]

    if not durations:
        return

    # Generate output file
    output_file = _generate_output_filename("slow_tests", ".txt")

    # Write the report
    lines = [f"slowest {len(durations)} durations", ""]
    for nodeid, duration in durations:
        lines.append(f"{duration:.4f}s {nodeid}")

    output_file.write_text("\n".join(lines))

    # Print single line indicating where the file was saved
    _print_lock_message(f"Slow tests report saved to: {output_file}")


def _write_coverage_summary_to_file(
    terminalreporter: "pytest.TerminalReporter",
    config: pytest.Config,
) -> None:
    """Write the full coverage report (term-missing format) to a file.

    This captures the same output that would be printed to terminal with
    --cov-report=term-missing and writes it to a file instead.
    """
    # Check if coverage plugin is active
    cov_plugin = config.pluginmanager.get_plugin("_cov")
    if cov_plugin is None:
        return

    # Get the coverage object from pytest-cov
    cov_controller = getattr(cov_plugin, "cov_controller", None)
    if cov_controller is None:
        return

    cov_obj = getattr(cov_controller, "cov", None)
    if cov_obj is None:
        return

    # Generate output file
    output_file = _generate_output_filename("coverage", ".txt")

    try:
        # Capture the full term-missing report to a StringIO
        report_output = StringIO()
        cov_obj.report(file=report_output, show_missing=True)
        report_content = report_output.getvalue()

        if report_content:
            output_file.write_text(report_content)
            _print_lock_message(f"Coverage report saved to: {output_file}")
    except CoverageException:
        # If we can't generate the report, don't create an empty file
        pass


def _print_test_durations_for_ci(
    terminalreporter: "pytest.TerminalReporter",
) -> None:
    """Print all test durations in pytest-split format when running in CI.

    Writes every test's duration to stderr (bypassing pytest output capture)
    in the same JSON format as .test_durations. This makes it easy to inspect
    per-split timing and periodically update the pytest-split timing data.
    """
    if "CI" not in os.environ:
        return

    all_durations = _collect_test_durations(terminalreporter)
    if not all_durations:
        return

    # Sort by duration (slowest first)
    durations = sorted(all_durations.items(), key=lambda x: x[1], reverse=True)

    output = json.dumps(dict(durations), indent=2)
    os.write(2, f"\n=== test durations (pytest-split format) ===\n{output}\n".encode())

    # Save to file for CI artifact collection
    output_file = _generate_output_filename("test_durations", ".json")
    output_file.write_text(output)
    _print_lock_message(f"Test durations saved to: {output_file}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_junit_test_id(request: pytest.FixtureRequest, record_xml_attribute) -> None:
    """Set JUnit XML name to the full test ID for exact matching with offload.

    Uses OFFLOAD_ROOT env var if set (for consistent paths in offload runs),
    otherwise falls back to pytest's nodeid directly.
    """
    offload_root = os.environ.get("OFFLOAD_ROOT")

    if offload_root:
        # Build full test ID: relative_path::class::method or relative_path::method
        fspath = str(request.node.fspath)
        rel_path = os.path.relpath(fspath, offload_root)
        nodeid_parts = request.node.nodeid.split("::")
        # nodeid_parts[0] is the file path (possibly different due to rootdir), [1:] is class/method
        test_id = "::".join([rel_path] + nodeid_parts[1:])
    else:
        test_id = request.node.nodeid

    record_xml_attribute("name", test_id)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_conftest_hooks(namespace: dict) -> None:
    """Register the common conftest hooks into the given namespace (typically globals()).

    Uses a module-level guard to ensure hooks are only registered once. When running
    from the monorepo root, both the root conftest.py and per-project conftest.py files
    are discovered by pytest. Without the guard, pytest_addoption would fail with
    duplicate option errors.

    The first conftest.py to call this function gets the hooks. Subsequent calls are no-ops.
    """
    global _registered
    if _registered:
        return
    _registered = True

    namespace["pytest_sessionstart"] = _pytest_sessionstart
    namespace["pytest_sessionfinish"] = _pytest_sessionfinish
    namespace["pytest_addoption"] = _pytest_addoption
    namespace["pytest_configure"] = _pytest_configure
    namespace["pytest_collection_modifyitems"] = _pytest_collection_modifyitems
    namespace["pytest_collection_finish"] = _pytest_collection_finish
    namespace["pytest_terminal_summary"] = _pytest_terminal_summary
    # Resource guard hooks are registered as a plugin by start_resource_guards()
    # during pytest_sessionstart, so they don't need to be injected here.
    # Register the JUnit test ID fixture (with public name for pytest discovery)
    namespace["set_junit_test_id"] = _set_junit_test_id
