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
import importlib.metadata
import inspect
import os
import shutil
import stat
import tempfile
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Iterable
from contextlib import contextmanager
from enum import StrEnum
from enum import auto
from functools import wraps
from pathlib import Path
from typing import Any
from typing import Protocol
from typing import TypeVar
from typing import assert_never
from unittest.mock import patch
from uuid import uuid4

import pluggy
import pytest

# Entry point group through which packages declare their resource guard
# registrations. See register_all_resource_guards() for usage.
RESOURCE_GUARDS_ENTRY_POINT_GROUP = "resource_guards"


class ResourceGuardError(Exception):
    """Base for everything the resource guard system raises."""


class ResourceGuardViolation(ResourceGuardError):
    """A test or fixture violated the resource guard invariants at runtime."""


class ResourceGuardMisconfiguration(ResourceGuardError):
    """@fixture_uses_resources was used incorrectly (empty/stacked/overridden)."""


@dataclasses.dataclass
class _PerTestGuardState:
    """Per-test state stashed on pytest.Item during the test lifecycle."""

    tracking_dir: str
    marks: set[str]
    covered_resources: set[str]
    env_patcher: patch.dict  # ty: ignore[invalid-type-form]


# Module-level state for resource guard wrappers. The wrapper directory is created
# once per session (by the controller or single process) and reused by xdist workers.
# _owns_guard_wrapper_dir tracks whether this process created the directory (and is
# therefore responsible for deleting it) vs merely reusing one inherited from a parent
# process via the _PYTEST_GUARD_WRAPPER_DIR env var.
# _session_env_patcher is the patch.dict that manages PATH and _PYTEST_GUARD_WRAPPER_DIR;
# stopping it automatically restores PATH to its original value.
# _binary_guarded_resources is populated only by register_resource_guard() and drives
# PATH wrapper script creation. _guarded_resources is the union of binary and SDK
# guard names (populated by register_resource_guard() and register_sdk_guard()); it
# drives pytest mark registration, per-test env var setup, and violation checks. The
# distinction matters so we only create wrapper scripts for names that were meant to
# guard a real binary -- SDK-only names must not produce stray wrapper scripts.
_guard_wrapper_dir: str | None = None
_owns_guard_wrapper_dir: bool = False
_session_env_patcher: patch.dict | None = None  # ty: ignore[invalid-type-form]
_binary_guarded_resources: list[str] = []
_guarded_resources: list[str] = []

# Module-level state for the _ResourceGuardPlugin registration. Mirrors the
# _owns_guard_wrapper_dir pattern: start_resource_guards() only records a plugin
# here when it actually registered a new one on the session's pluginmanager, and
# stop_resource_guards() only unregisters when this ownership flag is set. This
# keeps start/stop symmetric even when start is called with a pluginmanager that
# already has a plugin registered (from a parent conftest / outer session), which
# would otherwise let the stop half of a self-test rip the outer session's hooks
# out and leave every subsequent test without its runtest_setup/teardown.
_owns_guard_plugin: bool = False
_guard_plugin: "_ResourceGuardPlugin | None" = None
_guard_plugin_manager: pluggy.PluginManager | None = None

# Module-level state for SDK guards. Each entry is (name, install_fn, cleanup_fn).
# Populated by register_sdk_guard() before create_sdk_resource_guards() runs.
_registered_sdk_guards: list[tuple[str, Callable[[], None], Callable[[], None]]] = []


def register_resource_guard(name: str) -> None:
    """Register a binary to be guarded by PATH wrapper scripts.

    The resource name must correspond to both a binary on PATH and a pytest
    mark name (e.g., register_resource_guard("tmux") guards the tmux binary
    and enforces @pytest.mark.tmux). Call register_guarded_resource_markers()
    from pytest_configure to register the corresponding pytest marks.

    Duplicate registrations are ignored.
    """
    if name not in _binary_guarded_resources:
        _binary_guarded_resources.append(name)
    if name not in _guarded_resources:
        _guarded_resources.append(name)


def get_guarded_resource_names() -> tuple[str, ...]:
    """Return the guarded resource names (binary + SDK guards)."""
    return tuple(_guarded_resources)


def register_all_resource_guards(
    entry_points: Callable[..., Iterable[Any]] = importlib.metadata.entry_points,
) -> None:
    """Register every guard declared via the resource_guards entry point group.

    Each entry point's value must be a callable that takes no arguments and
    registers one or more guards via register_resource_guard() and/or
    register_sdk_guard()/create_sdk_method_guard(). This is the canonical way
    for libraries in the monorepo to advertise their guards: the set of
    guarded resources is a global property, so projects don't need to
    re-declare it in their conftest.py.

    Safe to call multiple times: every individual registration function below
    deduplicates by guard name. The entry_points argument is dependency-
    injected to keep the test path free of importlib monkeypatching; callers
    should leave the default in place.
    """
    for entry_point in entry_points(group=RESOURCE_GUARDS_ENTRY_POINT_GROUP):
        register_fn = entry_point.load()
        register_fn()


def register_guarded_resource_markers(
    config: pytest.Config,
    *,
    skip_names: set[str] | None = None,
) -> None:
    """Register pytest markers for all guarded resources.

    Call this from pytest_configure to register marks for every resource
    registered via register_resource_guard() or register_sdk_guard().

    Resources that overlap with existing markers can be skipped via skip_names.
    """
    skip = skip_names or set()
    for name in _guarded_resources:
        if name not in skip:
            config.addinivalue_line(
                "markers",
                f"{name}: marks tests that use the {name} resource",
            )


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
    """Create wrapper scripts for binary-guarded resources and prepend to PATH.

    Each wrapper intercepts calls to the corresponding binary and enforces
    that the test has the appropriate pytest mark. The list of resources
    comes from prior register_resource_guard() calls; SDK-only guards are
    intentionally excluded so they do not produce stray wrapper scripts
    named after internal SDK identifiers.

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

    for resource in _binary_guarded_resources:
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

    Adds the guard name to _guarded_resources and defers the install/cleanup
    functions to create_sdk_resource_guards().
    """
    registered_names = {entry[0] for entry in _registered_sdk_guards}
    if name not in registered_names:
        _registered_sdk_guards.append((name, install, cleanup))
        if name not in _guarded_resources:
            _guarded_resources.append(name)


class MethodKind(StrEnum):
    """How to wrap a guarded method."""

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
    """Install all registered SDK guards.

    Iterates through guards registered via register_sdk_guard() and calls
    each install function.
    """
    for _name, install, _cleanup in _registered_sdk_guards:
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
    parent conftest.py), the call is a no-op for plugin registration,
    and the matching stop_resource_guards() will NOT unregister that
    pre-existing plugin -- ownership is tracked per caller.
    """
    global _owns_guard_plugin, _guard_plugin, _guard_plugin_manager

    create_resource_guard_wrappers()
    create_sdk_resource_guards()
    if session.config.pluginmanager.get_plugin("resource_guards") is None:
        plugin = _ResourceGuardPlugin()
        session.config.pluginmanager.register(plugin, "resource_guards")
        _owns_guard_plugin = True
        _guard_plugin = plugin
        _guard_plugin_manager = session.config.pluginmanager


def stop_resource_guards() -> None:
    """Clean up all resource guards (SDK monkeypatches and binary wrappers).

    Call this from pytest_sessionfinish. Reverses start_resource_guards(),
    including unregistering the _ResourceGuardPlugin iff this call to start
    was the one that registered it.
    """
    global _owns_guard_plugin, _guard_plugin, _guard_plugin_manager

    # Only the caller that registered the plugin clears its own bookkeeping.
    # A non-owner stop must leave _owns_guard_plugin / _guard_plugin /
    # _guard_plugin_manager untouched so the real owner's later stop still
    # finds the state it needs to unregister the plugin. Mirrors the
    # _owns_guard_wrapper_dir handling in cleanup_resource_guard_wrappers().
    if _owns_guard_plugin and _guard_plugin is not None and _guard_plugin_manager is not None:
        _guard_plugin_manager.unregister(_guard_plugin)
        _owns_guard_plugin = False
        _guard_plugin = None
        _guard_plugin_manager = None

    cleanup_sdk_resource_guards()
    cleanup_resource_guard_wrappers()


# ---------------------------------------------------------------------------
# Pytest hook implementations
# ---------------------------------------------------------------------------


def _build_guard_env(marks: set[str], tracking_dir: str) -> dict[str, str]:
    """Build the guard env var dict for a per-test or per-fixture scope.

    ``marks`` is the set of resources the scope is authorized to use --
    pytest marks for tests, @fixture_uses_resources declarations for fixtures.
    """
    env: dict[str, str] = {
        "_PYTEST_GUARD_PHASE": "call",
        "_PYTEST_GUARD_TRACKING_DIR": tracking_dir,
    }
    for resource in _guarded_resources:
        env[f"_PYTEST_GUARD_{resource.upper()}"] = "allow" if resource in marks else "block"
    return env


class _GuardViolationKind(StrEnum):
    """What kind of resource guard invariant was violated."""

    BLOCKED = auto()
    NEVER_INVOKED = auto()


@dataclasses.dataclass(frozen=True)
class _GuardViolation:
    """A detected resource guard violation against a tracking_dir."""

    resource: str
    kind: _GuardViolationKind


def _detect_guard_violations(
    marks: set[str],
    tracking_dir: str,
    *,
    check_never_invoked: bool,
) -> _GuardViolation | None:
    """Detect blocked-invocation and superfluous-mark violations.

    Shared by the per-test and per-fixture guard checks. Returns the first
    violation found, or None if the scope is clean. ``check_never_invoked``
    should be False when the scope already failed for an unrelated reason --
    a missing tracking file there is most likely a downstream consequence,
    not the root cause.
    """
    for resource in _guarded_resources:
        if (Path(tracking_dir) / f"blocked_{resource}").exists():
            return _GuardViolation(resource=resource, kind=_GuardViolationKind.BLOCKED)

    if not check_never_invoked:
        return None

    for resource in _guarded_resources:
        if resource in marks and not (Path(tracking_dir) / resource).exists():
            return _GuardViolation(resource=resource, kind=_GuardViolationKind.NEVER_INVOKED)

    return None


# ---------------------------------------------------------------------------
# Fixture-level resource guard scope (opt-in)
# ---------------------------------------------------------------------------
#
# @fixture_uses_resources declares which resources a fixture itself uses.
# Such fixtures run setup/teardown under their own guard scope rather than
# attributing resource calls to whichever test triggered the setup -- which
# matters for module/session-scoped fixtures shared across tests. Untagged
# fixtures keep today's per-test attribution behavior.


_fixture_resource_marks: dict[Callable[..., Any], set[str]] = {}


class _NamedCallable(Protocol):
    """A callable that also carries a ``__name__`` (i.e. a function), which is
    all that ``fixture_uses_resources`` decorates."""

    __name__: str

    def __call__(self, *args: Any, **kwargs: Any) -> Any: ...


F = TypeVar("F", bound=_NamedCallable)


def fixture_uses_resources(*resources: str) -> Callable[[F], F]:
    """Declare which guarded resources a pytest fixture uses.

    Apply BELOW @pytest.fixture so the underlying function is registered
    before pytest captures it as a FixtureDef. Pass every resource the
    fixture invokes in a single call::

        @pytest.fixture(scope="module")
        @fixture_uses_resources("modal", "docker")
        def deployed_function() -> Generator[...]:
            ...

    During the fixture's setup and teardown, the resource guard treats the
    fixture as an independently-marked scope: resource calls inside the
    fixture are authorized against the fixture's declared resources rather
    than whichever test happens to trigger setup. Consuming tests must
    still carry @pytest.mark.<resource> for each declared resource (the
    static check in _collect_fixture_covered_resources enforces this).

    Raises ResourceGuardMisconfiguration if applied more than once to the
    same function -- stacking the decorator is not supported; combine all
    resources into the single call instead.
    """

    def decorator(func: F) -> F:
        if func in _fixture_resource_marks:
            raise ResourceGuardMisconfiguration(
                f"@fixture_uses_resources applied more than once to '{func.__name__}'. "
                f"Stacking the decorator is not supported -- combine all resources into "
                f"a single call, e.g. @fixture_uses_resources('a', 'b')."
            )
        _fixture_resource_marks[func] = set(resources)
        return func

    return decorator


def _collect_fixture_covered_resources(item: pytest.Item) -> set[str]:
    """Resources declared via @fixture_uses_resources by any fixture in item's closure.

    A test's @pytest.mark.<resource> is considered satisfied by transitive use
    if a fixture in the test's closure declared that resource via
    @fixture_uses_resources -- the fixture's setup is independently verified
    to actually invoke the resource, so the mark on the consuming test is
    meaningful even when the test body never calls the resource directly.

    Lazy fixtures retrieved via request.getfixturevalue() are not part of
    the static closure and therefore do not contribute to coverage here.

    Raises ResourceGuardMisconfiguration if a fixture name in the closure
    has multiple FixtureDefs (an override) where any def is tagged.
    Override semantics for @fixture_uses_resources are not supported.
    """
    fixture_info = item._fixtureinfo  # ty: ignore[unresolved-attribute]
    covered: set[str] = set()
    for name, fixturedefs in fixture_info.name2fixturedefs.items():
        tagged_decls: list[set[str]] = []
        for fixturedef in fixturedefs:
            declared = _fixture_resource_marks.get(fixturedef.func)
            if declared:
                tagged_decls.append(declared)
        if not tagged_decls:
            continue
        if len(fixturedefs) > 1:
            raise ResourceGuardMisconfiguration(
                f"RESOURCE GUARD: Fixture '{name}' has multiple definitions in the test's closure "
                f"(an override), and at least one is decorated with @fixture_uses_resources. "
                f"Override semantics for tagged fixtures are not supported -- remove the override "
                f"or remove the decorator from all defs."
            )
        for declared in tagged_decls:
            covered |= declared
    return covered


def _make_guarded_fixture_wrapper(
    original_func: Callable[..., Any],
    fixture_env: dict[str, str],
) -> Callable[..., Any]:
    """Wrap a fixture function so its setup and teardown run under fixture_env.

    Between the fixture's setup yield and teardown, env vars are restored so
    consuming tests see their own per-test env -- the fixture's env only
    applies inside the fixture function itself.

    Generator fixtures: setup runs up to the yield with fixture_env active,
    then env is restored. When pytest re-enters the generator for teardown,
    fixture_env is reapplied for the post-yield body.

    Non-generator fixtures: fixture_env is active for the whole call.
    """
    if inspect.isgeneratorfunction(original_func):

        @wraps(original_func)
        def wrapped(*args: Any, **kwargs: Any) -> Generator[Any, None, None]:
            gen = original_func(*args, **kwargs)
            with patch.dict(os.environ, fixture_env):
                value = next(gen)
            yield value
            # Drain the generator's post-yield (teardown). Pytest fixtures yield
            # exactly once, so next(..., None) is enough -- it absorbs the
            # StopIteration that signals teardown completed normally and lets
            # any teardown-raised exception propagate.
            with patch.dict(os.environ, fixture_env):
                next(gen, None)

        return wrapped

    @wraps(original_func)
    def wrapped_plain(*args: Any, **kwargs: Any) -> Any:
        with patch.dict(os.environ, fixture_env):
            return original_func(*args, **kwargs)

    return wrapped_plain


def _raise_fixture_blocked(
    fixture_id: str,
    resource: str,
    setup_exception: BaseException | None,
) -> None:
    """Raise the BLOCKED-violation error for a fixture invoking an undeclared resource."""
    raise ResourceGuardViolation(
        f"RESOURCE GUARD: Fixture '{fixture_id}' invoked '{resource}' but did not declare it via "
        f"@fixture_uses_resources({resource!r}). Add the declaration or remove the {resource} usage."
    ) from setup_exception


def _check_fixture_blocked_after_setup(
    fixture_id: str,
    resources: set[str],
    tracking_dir: str,
    setup_exception: BaseException | None = None,
) -> None:
    """Raise immediately if the fixture's setup invoked an undeclared resource.

    Runs in the fixture-setup hookwrapper's finally clause so a broken
    setup fails fast -- pytest aborts before the fixture's teardown
    runs and before any consuming tests execute. Chains the original
    setup exception via ``from`` so the underlying failure is preserved
    in the traceback.

    Only checks BLOCKED; NEVER_INVOKED (and any teardown-phase BLOCKED)
    is deferred to _check_fixture_at_scope_end, which runs as a fixture
    finalizer after the wrapper's post-yield body has executed.
    """
    violation = _detect_guard_violations(resources, tracking_dir, check_never_invoked=False)
    if violation is None:
        return
    # check_never_invoked=False guarantees only BLOCKED is returned.
    assert violation.kind == _GuardViolationKind.BLOCKED
    _raise_fixture_blocked(fixture_id, violation.resource, setup_exception)


def _check_fixture_at_scope_end(
    fixture_id: str,
    resources: set[str],
    tracking_dir: str,
    setup_failed: bool,
) -> None:
    """Validate fixture-scope guard invariants after the wrapper's teardown completes.

    Registered as a fixture finalizer before pytest's teardown
    finalizer, so it runs LAST in LIFO order (after teardown writes
    its tracking files). Catches:
    - BLOCKED invocations during teardown (the setup-time check
      already handled setup-phase BLOCKED via
      _check_fixture_blocked_after_setup; this re-runs the detector
      to catch new tracking files written during teardown).
    - NEVER_INVOKED for any declared resource that was never used
      in setup or teardown.

    Skips both checks when setup_failed -- a never-invoked violation
    there may just be a downstream effect of the underlying failure,
    and a teardown-phase BLOCKED cannot occur because pytest does not
    run teardown when setup failed.
    """
    if setup_failed:
        return
    violation = _detect_guard_violations(resources, tracking_dir, check_never_invoked=True)
    if violation is None:
        return

    match violation.kind:
        case _GuardViolationKind.BLOCKED:
            _raise_fixture_blocked(fixture_id, violation.resource, None)
        case _GuardViolationKind.NEVER_INVOKED:
            raise ResourceGuardViolation(
                f"RESOURCE GUARD: Fixture '{fixture_id}' declared @fixture_uses_resources({violation.resource!r}) "
                f"but did not invoke {violation.resource} during setup or teardown. Remove the declaration "
                f"or ensure the fixture exercises {violation.resource}."
            )
        case _:  # pragma: no cover
            assert_never(violation.kind)


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
    ) -> Generator[None, pluggy.Result[pytest.TestReport], None]:
        yield from _pytest_runtest_makereport(item, call)

    @staticmethod
    @pytest.hookimpl(hookwrapper=True)
    def pytest_fixture_setup(
        fixturedef: Any,
        request: pytest.FixtureRequest,
    ) -> Generator[None, pluggy.Result[object], None]:
        yield from _pytest_fixture_setup(fixturedef, request)


@contextmanager
def _swapped_fixturedef_func(fixturedef: Any, new_func: Callable[..., Any]) -> Generator[None, None, None]:
    """Temporarily replace fixturedef.func, restoring the original on exit."""
    original = fixturedef.func
    fixturedef.func = new_func
    try:
        yield
    finally:
        fixturedef.func = original


@pytest.hookimpl(hookwrapper=True)
def _pytest_fixture_setup(
    fixturedef: Any,
    request: pytest.FixtureRequest,
) -> Generator[None, pluggy.Result[object], None]:
    """Apply a fixture-scope guard around fixtures that opted in.

    Only fires for fixtures decorated with @fixture_uses_resources. Other
    fixtures yield-through with no behavior change, so this is fully
    backward-compatible with existing fixtures.
    """
    resources = _fixture_resource_marks.get(fixturedef.func)
    if not resources:
        yield
        return

    resources_set = set(resources)
    # Use pytest's session-scoped tmp_path_factory so the tracking dir lives
    # under pytest's own /tmp/pytest-of-<user>/pytest-N/ tree. Pytest rotates
    # those between sessions, so we don't need our own finalizer.
    tmp_path_factory = request.getfixturevalue("tmp_path_factory")
    tracking_dir = str(tmp_path_factory.mktemp("guard_fixture", numbered=True))
    fixture_env = _build_guard_env(resources_set, tracking_dir)
    fixture_id = fixturedef.argname

    # Register the deferred at-scope-end check BEFORE pytest's setup
    # registers its teardown finalizer. Finalizers run in LIFO order, so
    # this one runs LAST -- after the wrapper's post-yield (teardown)
    # phase has executed and written any teardown-phase tracking files.
    # The closure reads `setup_failed` from the enclosing scope at
    # finalizer-call time, so it sees the value assigned below.
    setup_failed = False

    def _at_scope_end() -> None:
        _check_fixture_at_scope_end(fixture_id, resources_set, tracking_dir, setup_failed)

    request.addfinalizer(_at_scope_end)

    setup_exception: BaseException | None = None
    with _swapped_fixturedef_func(fixturedef, _make_guarded_fixture_wrapper(fixturedef.func, fixture_env)):
        try:
            outcome = yield
            if outcome.excinfo is not None:
                setup_failed = True
                # outcome.excinfo is a (type, value, traceback) triple from pluggy.
                # Capturing the exception instance lets us chain it onto any
                # ResourceGuardViolation we raise below, so the underlying setup
                # failure isn't silently dropped from the traceback.
                setup_exception = outcome.excinfo[1]
        finally:
            # Fast-fail BLOCKED check: if setup invoked an undeclared
            # resource, raise now so pytest aborts consuming tests with
            # a clear error. NEVER_INVOKED and any teardown-phase
            # BLOCKED are handled by _at_scope_end.
            #
            # If our BLOCKED check raises, flip setup_failed so the
            # deferred _at_scope_end skips its checks -- otherwise the
            # same blocked tracking file would surface twice (once
            # here, once via the deferred re-detection).
            try:
                _check_fixture_blocked_after_setup(
                    fixture_id, resources_set, tracking_dir, setup_exception=setup_exception
                )
            except ResourceGuardViolation:
                setup_failed = True
                raise


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
    env_patcher = patch.dict(os.environ, _build_guard_env(marks, tracking_dir))
    env_patcher.start()

    # Assign _guard_state before any code that can raise: teardown and
    # makereport both unconditionally read item._guard_state, and raising
    # before assignment would mask the underlying error with a cascading
    # AttributeError. covered_resources starts as an empty placeholder and
    # is filled in below; if _collect_fixture_covered_resources raises, the
    # test enters the "setup failed" path and _check_guard_violations (the
    # only consumer of covered_resources, runs on call-phase makereport
    # only) never executes on it -- so the placeholder is never actually
    # consulted on the failure path.
    state = _PerTestGuardState(
        tracking_dir=tracking_dir,
        marks=marks,
        covered_resources=set(),
        env_patcher=env_patcher,
    )
    item._guard_state = state  # ty: ignore[unresolved-attribute]

    # Defer any ResourceGuardMisconfiguration from closure inspection until
    # *after* the inner pytest_runtest_setup chain has run. Raising before
    # yield would short-circuit other plugins' setup (e.g. caplog), which
    # then crash in their teardown phase and bury the original error. By
    # holding the exception until after yield, the inner setup completes
    # normally, all plugins get a chance to install their state, and the
    # error still surfaces as a setup-phase error.
    try:
        state.covered_resources = _collect_fixture_covered_resources(item)
        closure_error: ResourceGuardMisconfiguration | None = None
    except ResourceGuardMisconfiguration as exc:
        closure_error = exc

    yield

    if closure_error is not None:
        raise closure_error


@pytest.hookimpl(hookwrapper=True)
def _pytest_runtest_teardown(item: pytest.Item) -> Generator[None, None, None]:
    """Clean up resource guard environment variables after teardown."""
    yield

    state: _PerTestGuardState = item._guard_state  # ty: ignore[unresolved-attribute]
    state.env_patcher.stop()


def _check_guard_violations(state: _PerTestGuardState, report: pytest.TestReport) -> None:
    """Check resource guard invariants after the call phase and mutate the report if violated.

    Three checks:
    1. Blocked invocations: a test without @pytest.mark.<resource> invoked
       the resource anyway. Checked regardless of pass/fail so the guard
       violation is visible even when the test fails for a downstream reason.
    2. Superfluous marks: a test has @pytest.mark.<resource> but the resource
       was never invoked. Only checked on passing tests. Marks whose resource
       is already covered by a @fixture_uses_resources fixture in the test's
       closure are excluded, so the mark is accepted either when the test
       body invokes the resource OR when a tagged fixture transitively does.
    3. Undeclared fixture coverage: the test consumes a fixture decorated with
       @fixture_uses_resources(<resource>) but is missing the corresponding
       @pytest.mark.<resource>. Static analysis of the fixture closure, so
       reported regardless of runtime outcome -- the mark is required so that
       `pytest -m <resource>` selects every test that transitively needs it.
    """
    enforce_marks = state.marks - state.covered_resources
    violation = _detect_guard_violations(enforce_marks, state.tracking_dir, check_never_invoked=report.passed)
    if violation is not None:
        match violation.kind:
            case _GuardViolationKind.BLOCKED:
                msg = (
                    f"RESOURCE GUARD: Test invoked '{violation.resource}' without @pytest.mark.{violation.resource}.\n"
                    f"Add @pytest.mark.{violation.resource} to the test, or remove the {violation.resource} usage."
                )
                if report.passed:
                    report.outcome = "failed"
                    report.longrepr = msg
                else:
                    report.longrepr = f"{report.longrepr}\n\n{msg}"
            case _GuardViolationKind.NEVER_INVOKED:
                report.outcome = "failed"
                report.longrepr = (
                    f"Test marked with @pytest.mark.{violation.resource} but never invoked {violation.resource}.\n"
                    f"Remove the mark or ensure the test exercises {violation.resource}."
                )
            case _:  # pragma: no cover
                assert_never(violation.kind)

    undeclared = sorted(state.covered_resources - state.marks)
    if undeclared:
        # Report every missing mark in one message so the user can fix them all
        # at once instead of rediscovering them one by one across reruns. This
        # check is independent of the runtime BLOCKED/NEVER_INVOKED checks
        # above (it's a static property of the fixture closure), so it always
        # runs and appends to whatever longrepr those checks may have set.
        lines = [
            f"RESOURCE GUARD: Test consumes a fixture decorated with @fixture_uses_resources({resource!r}) "
            f"but is missing @pytest.mark.{resource}. Add the mark so that `pytest -m {resource}` selects "
            f"this test alongside other {resource}-using tests."
            for resource in undeclared
        ]
        msg = "\n".join(lines)
        if report.outcome == "passed":
            report.outcome = "failed"
            report.longrepr = msg
        else:
            report.longrepr = f"{report.longrepr}\n\n{msg}"


@pytest.hookimpl(hookwrapper=True)
def _pytest_runtest_makereport(
    item: pytest.Item,
    call: pytest.CallInfo,
) -> Generator[None, pluggy.Result[pytest.TestReport], None]:
    """Enforce resource guard invariants after each test phase."""
    outcome = yield
    report = outcome.get_result()

    state: _PerTestGuardState = item._guard_state  # ty: ignore[unresolved-attribute]

    if call.when != "call":
        if call.when == "teardown":
            shutil.rmtree(state.tracking_dir, ignore_errors=True)
        return

    _check_guard_violations(state, report)
