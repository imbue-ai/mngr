"""Resource guard system for enforcing pytest marks on external tool usage.

Two guard mechanisms are provided:

1. PATH wrapper scripts intercept calls to guarded CLI binaries (e.g. tmux,
   rsync). During the test call phase, wrappers block or track invocations
   based on whether the test has the corresponding mark.

2. SDK monkeypatches intercept Python SDK chokepoints. SDK-specific guards
   are registered via register_sdk_guard() before session start, then
   installed by create_sdk_resource_guards(). The monkeypatches call
   enforce_sdk_guard, which mirrors the wrapper logic: block unmarked
   usage and track marked usage.

Both mechanisms use per-test tracking files so that makereport can fail tests
that invoke a resource without the mark or carry a mark without invoking it.

Usage:
    Register SDK guards via register_sdk_guard(name, install, cleanup) before
    pytest_sessionstart. Call create_resource_guard_wrappers(resources) and
    create_sdk_resource_guards() during pytest_sessionstart.
    Call cleanup_sdk_resource_guards() and cleanup_resource_guard_wrappers()
    during pytest_sessionfinish. Register the three runtest hooks
    (pytest_runtest_setup, pytest_runtest_teardown, pytest_runtest_makereport)
    into the conftest namespace.
"""

import os
import shutil
import stat
import tempfile
from collections.abc import Callable
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest


class ResourceGuardViolation(Exception):
    """Raised when a test invokes an SDK resource without the required mark."""


# Module-level state for resource guard wrappers. The wrapper directory is created
# once per session (by the controller or single process) and reused by xdist workers.
# _owns_guard_wrapper_dir tracks whether this process created the directory (and is
# therefore responsible for deleting it) vs merely reusing one inherited from a parent
# process via the _PYTEST_GUARD_WRAPPER_DIR env var.
# _session_env_patcher is the patch.dict that manages PATH and _PYTEST_GUARD_WRAPPER_DIR;
# stopping it automatically restores PATH to its original value.
# _guarded_resources is populated by register_resource_guard() and extended by
# create_sdk_resource_guards(); the hooks read from it at session start.
_guard_wrapper_dir: str | None = None
_owns_guard_wrapper_dir: bool = False
_session_env_patcher: patch.dict | None = None  # type: ignore[type-arg]
_guarded_resources: list[str] = []

# Module-level state for SDK guards. Each entry is (name, install_fn, cleanup_fn).
# Populated by register_sdk_guard() before create_sdk_resource_guards() runs.
_registered_sdk_guards: list[tuple[str, Callable[[], None], Callable[[], None]]] = []


def register_resource_guard(name: str) -> None:
    """Register a binary to be guarded by PATH wrapper scripts.

    Call this from each project's conftest.py before register_conftest_hooks().
    The resource name must correspond to both a binary on PATH and a pytest
    mark name (e.g., register_resource_guard("tmux") guards the tmux binary
    and enforces @pytest.mark.tmux).

    Duplicate registrations are ignored.
    """
    if name not in _guarded_resources:
        _guarded_resources.append(name)


def generate_wrapper_script(resource: str, real_path: str) -> str:
    """Generate a bash wrapper script for a guarded resource.

    The wrapper checks environment variables set by the pytest_runtest_setup hook:
    - _PYTEST_GUARD_PHASE: Set to "call" for the entire test lifecycle (setup
      through teardown). Outside the test lifecycle (e.g., during collection),
      this variable is unset and the wrapper delegates unconditionally.
    - _PYTEST_GUARD_<RESOURCE>: "block" if the test lacks the mark, "allow" if it has it
    - _PYTEST_GUARD_TRACKING_DIR: Directory where tracking files are created

    When guard env vars are active (during a test's lifecycle):
    - If the guard is "block", the wrapper records the violation, prints an error,
      and exits 127. The tracking file ensures makereport catches the missing mark
      even if the test handles the non-zero exit code gracefully.
    - If the guard is "allow", the wrapper touches a tracking file and delegates.
    When guard env vars are not active (outside test lifecycle), the wrapper
    always delegates to the real binary.
    """
    bash_guard_var = f"$_PYTEST_GUARD_{resource.upper()}"
    return f"""#!/bin/bash
if [ "$_PYTEST_GUARD_PHASE" = "call" ]; then
    if [ "{bash_guard_var}" = "block" ]; then
        if [ -n "$_PYTEST_GUARD_TRACKING_DIR" ]; then
            touch "$_PYTEST_GUARD_TRACKING_DIR/blocked_{resource}"
        fi
        echo "RESOURCE GUARD: Test invoked '{resource}' without @pytest.mark.{resource} mark." >&2
        echo "Add @pytest.mark.{resource} to the test, or remove the {resource} usage." >&2
        exit 127
    fi
    if [ "{bash_guard_var}" = "allow" ] && [ -n "$_PYTEST_GUARD_TRACKING_DIR" ]; then
        touch "$_PYTEST_GUARD_TRACKING_DIR/{resource}"
    fi
fi
exec "{real_path}" "$@"
"""


def generate_stub_wrapper_script(resource: str) -> str:
    """Generate a wrapper for a resource binary that is not installed.

    The stub still tracks blocked/allowed invocations for mark enforcement,
    but always exits 127 since there is no real binary to delegate to.
    This allows the guard system to work on machines where the binary is
    missing -- tests that need the resource will fail clearly, and mark
    enforcement still catches missing/superfluous marks.
    """
    bash_guard_var = f"$_PYTEST_GUARD_{resource.upper()}"
    return f"""#!/bin/bash
if [ "$_PYTEST_GUARD_PHASE" = "call" ]; then
    if [ "{bash_guard_var}" = "block" ]; then
        if [ -n "$_PYTEST_GUARD_TRACKING_DIR" ]; then
            touch "$_PYTEST_GUARD_TRACKING_DIR/blocked_{resource}"
        fi
        echo "RESOURCE GUARD: Test invoked '{resource}' without @pytest.mark.{resource} mark." >&2
        echo "Add @pytest.mark.{resource} to the test, or remove the {resource} usage." >&2
        exit 127
    fi
    if [ "{bash_guard_var}" = "allow" ] && [ -n "$_PYTEST_GUARD_TRACKING_DIR" ]; then
        touch "$_PYTEST_GUARD_TRACKING_DIR/{resource}"
    fi
fi
echo "RESOURCE GUARD: '{resource}' is not installed on this machine." >&2
exit 127
"""


def create_resource_guard_wrappers() -> None:
    """Create wrapper scripts for guarded resources and prepend to PATH.

    Each wrapper intercepts calls to the corresponding binary and enforces
    that the test has the appropriate pytest mark. The list of resources
    comes from prior register_resource_guard() calls.

    For xdist: the controller creates the wrappers and modifies PATH. Workers
    inherit the modified PATH and wrapper directory via environment variables.
    The _PYTEST_GUARD_WRAPPER_DIR env var signals that wrappers already exist.

    Uses patch.dict to manage PATH and _PYTEST_GUARD_WRAPPER_DIR so that
    cleanup_resource_guard_wrappers can restore everything by calling .stop().
    """
    global _guard_wrapper_dir, _owns_guard_wrapper_dir, _session_env_patcher

    # If wrappers already exist (e.g., inherited from xdist controller), reuse them.
    existing_dir = os.environ.get("_PYTEST_GUARD_WRAPPER_DIR")
    if existing_dir and Path(existing_dir).is_dir():
        _guard_wrapper_dir = existing_dir
        _owns_guard_wrapper_dir = False
        return

    _guard_wrapper_dir = tempfile.mkdtemp(prefix="pytest_resource_guards_")
    _owns_guard_wrapper_dir = True

    for resource in _guarded_resources:
        real_path = shutil.which(resource)
        wrapper_path = Path(_guard_wrapper_dir) / resource
        if real_path is not None:
            wrapper_path.write_text(generate_wrapper_script(resource, real_path))
        else:
            wrapper_path.write_text(generate_stub_wrapper_script(resource))
        wrapper_path.chmod(wrapper_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # Prepend wrapper directory to PATH and advertise to xdist workers.
    # patch.dict saves the original PATH and restores it when stopped.
    original_path = os.environ.get("PATH", "")
    _session_env_patcher = patch.dict(
        os.environ,
        {
            "PATH": f"{_guard_wrapper_dir}{os.pathsep}{original_path}",
            "_PYTEST_GUARD_WRAPPER_DIR": _guard_wrapper_dir,
        },
    )
    _session_env_patcher.start()


def cleanup_resource_guard_wrappers() -> None:
    """Remove wrapper scripts and restore PATH.

    Only the process that created the wrappers should delete them.  Processes
    that merely reused an existing wrapper directory (e.g. xdist workers) just
    clear their local reference.
    """
    global _guard_wrapper_dir, _owns_guard_wrapper_dir, _session_env_patcher

    if not _owns_guard_wrapper_dir:
        _guard_wrapper_dir = None
        return

    if _guard_wrapper_dir is not None:
        shutil.rmtree(_guard_wrapper_dir, ignore_errors=True)
        _guard_wrapper_dir = None

    # Stopping the patcher restores PATH and removes _PYTEST_GUARD_WRAPPER_DIR.
    if _session_env_patcher is not None:
        _session_env_patcher.stop()
        _session_env_patcher = None

    _owns_guard_wrapper_dir = False


# ---------------------------------------------------------------------------
# SDK resource guards (monkeypatch-based, for Python SDK chokepoints)
# ---------------------------------------------------------------------------


def enforce_sdk_guard(resource: str) -> None:
    """Check SDK resource guard env vars and enforce/track usage.

    Mirrors the bash wrapper logic for binary guards, but called from Python.
    During the test call phase:
    - If blocked: creates tracking file and raises ResourceGuardViolation
    - If allowed: creates tracking file to confirm the resource was used
    Outside the call phase (fixture setup/teardown), does nothing.
    """
    if os.environ.get("_PYTEST_GUARD_PHASE") != "call":
        return

    guard_status = os.environ.get(f"_PYTEST_GUARD_{resource.upper()}")
    tracking_dir = os.environ.get("_PYTEST_GUARD_TRACKING_DIR")

    if guard_status == "block":
        if tracking_dir:
            Path(tracking_dir).joinpath(f"blocked_{resource}").touch()
        raise ResourceGuardViolation(
            f"RESOURCE GUARD: Test invoked '{resource}' without @pytest.mark.{resource} mark.\n"
            f"Add @pytest.mark.{resource} to the test, or remove the {resource} usage."
        )

    if guard_status == "allow" and tracking_dir:
        Path(tracking_dir).joinpath(resource).touch()


def register_sdk_guard(
    name: str,
    install: Callable[[], None],
    cleanup: Callable[[], None],
) -> None:
    """Register an SDK guard for use by create_sdk_resource_guards.

    Callers (e.g. mng's register_guards module) call this before
    register_conftest_hooks() to push SDK-specific guard implementations
    into the infrastructure. Deduplicates by name so multiple conftest
    files can safely call the registration function.
    """
    registered_names = {entry[0] for entry in _registered_sdk_guards}
    if name not in registered_names:
        _registered_sdk_guards.append((name, install, cleanup))


def create_sdk_resource_guards() -> None:
    """Install all registered SDK guards and add their names to _guarded_resources.

    Iterates through guards registered via register_sdk_guard(), calls each
    install function, and extends _guarded_resources so the per-test hooks
    set up env vars for them.
    """
    for name, install, _cleanup in _registered_sdk_guards:
        if name not in _guarded_resources:
            _guarded_resources.append(name)
        install()


def cleanup_sdk_resource_guards() -> None:
    """Call cleanup for all registered SDK guards."""
    for _name, _install, cleanup in _registered_sdk_guards:
        cleanup()


# ---------------------------------------------------------------------------
# Pytest hook implementations (prefixed with _ to avoid accidental discovery)
# ---------------------------------------------------------------------------


def _build_per_test_guard_env(marks: set[str], tracking_dir: str) -> dict[str, str]:
    """Build the env var dict for a single test's resource guards."""
    env: dict[str, str] = {
        "_PYTEST_GUARD_PHASE": "call",
        "_PYTEST_GUARD_TRACKING_DIR": tracking_dir,
    }
    for resource in _guarded_resources:
        env[f"_PYTEST_GUARD_{resource.upper()}"] = "allow" if resource in marks else "block"
    return env


@pytest.hookimpl(hookwrapper=True)
def _pytest_runtest_setup(item: pytest.Item) -> Generator[None, None, None]:
    """Activate resource guards for the entire test lifecycle.

    Guards are active during setup, call, and teardown. If a test uses a
    resource (directly or via fixtures), it needs the corresponding mark.

    Setting vars early also ensures fixtures that snapshot os.environ
    (like get_subprocess_test_env) capture the guard configuration.

    Uses patch.dict to manage env vars so cleanup is automatic and the
    set of vars added in setup can never drift from what teardown removes.
    """
    if _guard_wrapper_dir is None:
        yield
        return

    marks = {m.name for m in item.iter_markers()}

    # Create per-test tracking directory
    tracking_dir = tempfile.mkdtemp(prefix="pytest_guard_track_")
    setattr(item, "_resource_tracking_dir", tracking_dir)  # noqa: B010
    setattr(item, "_resource_marks", marks)  # noqa: B010

    # Start a patch.dict that will be stopped in teardown
    patcher = patch.dict(os.environ, _build_per_test_guard_env(marks, tracking_dir))
    patcher.start()
    setattr(item, "_guard_env_patcher", patcher)  # noqa: B010

    yield


@pytest.hookimpl(hookwrapper=True)
def _pytest_runtest_teardown(item: pytest.Item) -> Generator[None, None, None]:
    """Clean up resource guard environment variables after teardown."""
    yield

    patcher = getattr(item, "_guard_env_patcher", None)
    if patcher is not None:
        patcher.stop()


@pytest.hookimpl(hookwrapper=True)
def _pytest_runtest_makereport(
    item: pytest.Item,
    call: pytest.CallInfo,  # type: ignore[type-arg]
) -> Generator[None, None, None]:
    """Enforce resource guard invariants after each test.

    After the call phase, checks two things:
    1. Blocked invocations: if a test without @pytest.mark.<resource> invoked
       the resource anyway (and handled the non-zero exit or caught the
       ResourceGuardViolation), the blocked_<resource> tracking file catches it.
       This check runs regardless of test pass/fail so that the guard violation
       is visible even when the test fails for a downstream reason.
    2. Superfluous marks: if a test has @pytest.mark.<resource> but the resource
       was never invoked, the test is failed. Only checked on passing tests.
    """
    outcome = yield
    report = outcome.get_result()

    if call.when != "call":
        # Clean up tracking dir on the final phase (teardown)
        if call.when == "teardown":
            tracking_dir = getattr(item, "_resource_tracking_dir", None)
            if tracking_dir:
                shutil.rmtree(tracking_dir, ignore_errors=True)
        return

    tracking_dir = getattr(item, "_resource_tracking_dir", None)
    if tracking_dir is None:
        return

    marks: set[str] = getattr(item, "_resource_marks", set())

    # Check for blocked invocations regardless of pass/fail. When a guard
    # blocks a resource inside a subprocess (e.g., mng create -> tmux), the
    # test often fails for a downstream reason ("Agent is stopped") that
    # obscures the real cause. Surfacing the guard violation makes it clear.
    for resource in _guarded_resources:
        blocked_file = Path(tracking_dir) / f"blocked_{resource}"
        if blocked_file.exists():
            msg = (
                f"RESOURCE GUARD: Test invoked '{resource}' without @pytest.mark.{resource}.\n"
                f"Add @pytest.mark.{resource} to the test, or remove the {resource} usage."
            )
            if report.passed:
                report.outcome = "failed"
                report.longrepr = msg
            else:
                # Append guard info to the existing failure so the root cause is visible.
                report.longrepr = f"{report.longrepr}\n\n{msg}"
            return

    # Superfluous mark check only matters if the test passed.
    if not report.passed:
        return

    for resource in _guarded_resources:
        if resource in marks:
            tracking_file = Path(tracking_dir) / resource
            if not tracking_file.exists():
                report.outcome = "failed"
                report.longrepr = (
                    f"Test marked with @pytest.mark.{resource} but never invoked {resource}.\n"
                    f"Remove the mark or ensure the test exercises {resource}."
                )
                return
