"""Resource guard system for enforcing pytest marks on external tool usage.

Two guard mechanisms are provided:

1. PATH wrapper scripts intercept calls to guarded CLI binaries (e.g. tmux,
   rsync). During the test call phase, wrappers block or track invocations
   based on whether the test has the corresponding mark.

2. SDK monkeypatches intercept Python SDK chokepoints. SDK-specific guards
   are registered via register_sdk_guard() or create_sdk_method_guard()
   before session start, then installed during start_resource_guards().
   The monkeypatches call enforce_sdk_guard, which mirrors the wrapper
   logic: block unmarked usage and track marked usage.

Both mechanisms use per-test tracking files so that makereport can fail tests
that invoke a resource without the mark or carry a mark without invoking it.

Usage:
    Register binary guards via register_resource_guard(name) and SDK guards
    via register_sdk_guard(name, install, cleanup) or
    create_sdk_method_guard(name, methods) before pytest_sessionstart.
    Call start_resource_guards(session) during pytest_sessionstart and
    stop_resource_guards() during pytest_sessionfinish. The per-test hooks
    are registered automatically as a pytest plugin by start_resource_guards().
"""

import dataclasses
import os
import shutil
import stat
import tempfile
from collections.abc import Callable
from collections.abc import Generator
from enum import StrEnum
from enum import auto
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest


class ResourceGuardViolation(Exception):
    """Raised when a test invokes an SDK resource without the required mark."""


@dataclasses.dataclass
class _PerTestGuardState:
    """Per-test state stashed on pytest.Item during the test lifecycle."""

    tracking_dir: str
    marks: set[str]
    env_patcher: patch.dict  # ty: ignore[invalid-type-form]


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
_session_env_patcher: patch.dict | None = None  # ty: ignore[invalid-type-form]
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

    Callers (e.g. resource-guards-modal, resource-guards-docker) call this
    before register_conftest_hooks() to push SDK-specific guard
    implementations into the infrastructure. Deduplicates by name so
    multiple conftest files can safely call the registration function.
    """
    registered_names = {entry[0] for entry in _registered_sdk_guards}
    if name not in registered_names:
        _registered_sdk_guards.append((name, install, cleanup))


class MethodKind(StrEnum):
    """How to wrap a guarded method."""

    @staticmethod
    def _generate_next_value_(name: str, start: int, count: int, last_values: list[str]) -> str:
        return name.upper()

    SYNC = auto()
    ASYNC = auto()
    ASYNC_GEN = auto()


def _make_sync_wrapper(name: str, originals: dict[str, Any], key: str) -> Callable[..., Any]:
    def guarded(self, *args, **kwargs):
        enforce_sdk_guard(name)
        return originals[key](self, *args, **kwargs)

    return guarded


def _make_async_wrapper(name: str, originals: dict[str, Any], key: str) -> Callable[..., Any]:
    async def guarded(self, *args, **kwargs):
        enforce_sdk_guard(name)
        return await originals[key](self, *args, **kwargs)

    return guarded


def _make_async_gen_wrapper(name: str, originals: dict[str, Any], key: str) -> Callable[..., Any]:
    async def guarded(self, *args, **kwargs):
        enforce_sdk_guard(name)
        async for item in originals[key](self, *args, **kwargs):
            yield item

    return guarded


_WRAPPER_FACTORIES: dict[str, Callable[[str, dict[str, Any], str], Callable[..., Any]]] = {
    MethodKind.SYNC: _make_sync_wrapper,
    MethodKind.ASYNC: _make_async_wrapper,
    MethodKind.ASYNC_GEN: _make_async_gen_wrapper,
}


def create_sdk_method_guard(
    name: str,
    methods: list[tuple[type, str, MethodKind]],
) -> None:
    """Register an SDK guard that monkeypatches one or more methods on classes.

    Each entry in methods is (class, method_name, kind) where kind is one of
    MethodKind.SYNC, MethodKind.ASYNC, or MethodKind.ASYNC_GEN.

    Example:
        create_sdk_method_guard("my_sdk", [
            (SomeClient, "send", MethodKind.SYNC),
        ])
    """
    originals: dict[str, Any] = {}
    patches: list[tuple[type, str, str, MethodKind]] = []  # (cls, method_name, key, kind)

    for cls, method_name, kind in methods:
        key = uuid4().hex
        patches.append((cls, method_name, key, kind))

    def install() -> None:
        for cls, method_name, key, kind in patches:
            originals[key] = getattr(cls, method_name)
            setattr(cls, method_name, _WRAPPER_FACTORIES[kind](name, originals, key))

    def cleanup() -> None:
        for cls, method_name, key, _kind in patches:
            if key in originals:
                setattr(cls, method_name, originals[key])
        originals.clear()

    register_sdk_guard(name, install, cleanup)


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


def start_resource_guards(session: pytest.Session) -> None:
    """Create all resource guards and register per-test hooks.

    Call this from pytest_sessionstart. Handles binary wrappers, SDK
    monkeypatches, and hook registration in one call. Safe to call with
    only binary guards, only SDK guards, or both registered.

    Idempotent: if the guard plugin is already registered (e.g., from a
    parent conftest.py), the call is a no-op for plugin registration.
    """
    create_resource_guard_wrappers()
    create_sdk_resource_guards()
    if session.config.pluginmanager.get_plugin("resource_guards") is None:
        session.config.pluginmanager.register(_ResourceGuardPlugin(), "resource_guards")


def stop_resource_guards() -> None:
    """Clean up all resource guards (SDK monkeypatches and binary wrappers).

    Call this from pytest_sessionfinish. Reverses start_resource_guards().
    """
    cleanup_sdk_resource_guards()
    cleanup_resource_guard_wrappers()


# ---------------------------------------------------------------------------
# Pytest hook implementations
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


class _ResourceGuardPlugin:
    """Pytest plugin registered by start_resource_guards().

    Encapsulates the per-test hooks so they coexist naturally with any
    hooks defined in the consumer's conftest.py.
    """

    @staticmethod
    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_setup(item: pytest.Item) -> Generator[None, None, None]:
        yield from _pytest_runtest_setup(item)

    @staticmethod
    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_teardown(item: pytest.Item) -> Generator[None, None, None]:
        yield from _pytest_runtest_teardown(item)

    @staticmethod
    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(
        item: pytest.Item,
        call: pytest.CallInfo,
    ) -> Generator[None, None, None]:
        yield from _pytest_runtest_makereport(item, call)


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
    assert _guard_wrapper_dir is not None, (
        "Resource guard hooks are registered but create_resource_guard_wrappers() was never called. "
        "Call create_resource_guard_wrappers() in pytest_sessionstart before tests run."
    )

    marks = {m.name for m in item.iter_markers()}
    tracking_dir = tempfile.mkdtemp(prefix="pytest_guard_track_")
    env_patcher = patch.dict(os.environ, _build_per_test_guard_env(marks, tracking_dir))
    env_patcher.start()

    item._guard_state = _PerTestGuardState(  # ty: ignore[unresolved-attribute]
        tracking_dir=tracking_dir,
        marks=marks,
        env_patcher=env_patcher,
    )

    yield


@pytest.hookimpl(hookwrapper=True)
def _pytest_runtest_teardown(item: pytest.Item) -> Generator[None, None, None]:
    """Clean up resource guard environment variables after teardown."""
    yield

    state: _PerTestGuardState = item._guard_state  # ty: ignore[unresolved-attribute]
    state.env_patcher.stop()


def _check_guard_violations(state: _PerTestGuardState, report: pytest.TestReport) -> None:
    """Check resource guard invariants after the call phase and mutate the report if violated.

    Two checks:
    1. Blocked invocations: a test without @pytest.mark.<resource> invoked
       the resource anyway. Checked regardless of pass/fail so the guard
       violation is visible even when the test fails for a downstream reason.
    2. Superfluous marks: a test has @pytest.mark.<resource> but the resource
       was never invoked. Only checked on passing tests.
    """
    tracking_dir = state.tracking_dir
    marks = state.marks

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
                report.longrepr = f"{report.longrepr}\n\n{msg}"
            return

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


@pytest.hookimpl(hookwrapper=True)
def _pytest_runtest_makereport(
    item: pytest.Item,
    call: pytest.CallInfo,
) -> Generator[None, None, None]:
    """Enforce resource guard invariants after each test phase."""
    outcome = yield
    report = outcome.get_result()

    state: _PerTestGuardState = item._guard_state  # ty: ignore[unresolved-attribute]

    if call.when != "call":
        if call.when == "teardown":
            shutil.rmtree(state.tracking_dir, ignore_errors=True)
        return

    _check_guard_violations(state, report)
