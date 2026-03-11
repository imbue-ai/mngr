import asyncio
import os
from collections.abc import Callable
from pathlib import Path

import pytest

import imbue.resource_guards.resource_guards as resource_guards
from imbue.resource_guards.resource_guards import MethodKind
from imbue.resource_guards.resource_guards import ResourceGuardViolation
from imbue.resource_guards.resource_guards import _PerTestGuardState
from imbue.resource_guards.resource_guards import _build_per_test_guard_env
from imbue.resource_guards.resource_guards import _check_guard_violations
from imbue.resource_guards.resource_guards import cleanup_resource_guard_wrappers
from imbue.resource_guards.resource_guards import cleanup_sdk_resource_guards
from imbue.resource_guards.resource_guards import create_resource_guard_wrappers
from imbue.resource_guards.resource_guards import create_sdk_method_guard
from imbue.resource_guards.resource_guards import create_sdk_resource_guards
from imbue.resource_guards.resource_guards import enforce_sdk_guard
from imbue.resource_guards.resource_guards import generate_stub_wrapper_script
from imbue.resource_guards.resource_guards import generate_wrapper_script
from imbue.resource_guards.resource_guards import register_resource_guard
from imbue.resource_guards.resource_guards import register_sdk_guard
from imbue.resource_guards.resource_guards import start_resource_guards
from imbue.resource_guards.resource_guards import stop_resource_guards

# Use ubiquitous coreutils binaries so these tests run on any system.
_TEST_RESOURCES = ["echo", "cat", "ls"]

# Conftest that pytester injects into its temp directory.  It registers the
# resource guard hooks for "cat" only, which is enough for end-to-end tests.
# cat is a good choice: `cat /dev/null` succeeds, `cat /nonexistent` fails.
_PYTESTER_CONFTEST = """\
import os
from imbue.resource_guards.resource_guards import (
    register_resource_guard,
    start_resource_guards,
    stop_resource_guards,
)

# Clear inherited guard state so we create fresh wrappers for our resources.
os.environ.pop("_PYTEST_GUARD_WRAPPER_DIR", None)

register_resource_guard("cat")

def pytest_configure(config):
    config.addinivalue_line("markers", "cat: test uses cat")

def pytest_sessionstart(session):
    start_resource_guards(session)

def pytest_sessionfinish(session, exitstatus):
    stop_resource_guards()
"""

pytest_plugins = ["pytester"]


# ---------------------------------------------------------------------------
# Script generation (unit tests)
# ---------------------------------------------------------------------------


def test_generate_stub_wrapper_script_contains_shebang_and_exit() -> None:
    script = generate_stub_wrapper_script("mybin")
    assert script.startswith("#!/bin/bash\n")
    assert "not installed on this machine" in script
    assert "exit 127" in script
    assert "$_PYTEST_GUARD_MYBIN" in script


def test_generate_wrapper_script_contains_shebang_and_exec() -> None:
    script = generate_wrapper_script("mybin", "/usr/bin/mybin")
    assert script.startswith("#!/bin/bash\n")
    assert 'exec "/usr/bin/mybin" "$@"' in script


def test_generate_wrapper_script_contains_guard_check() -> None:
    script = generate_wrapper_script("mybin", "/usr/bin/mybin")
    assert "$_PYTEST_GUARD_MYBIN" in script
    assert "@pytest.mark.mybin" in script
    assert '"block"' in script
    assert '"allow"' in script


# ---------------------------------------------------------------------------
# End-to-end guard behavior (pytester)
# ---------------------------------------------------------------------------


def test_marked_test_that_calls_resource_passes(pytester: pytest.Pytester) -> None:
    """A test with @pytest.mark.cat that calls cat should pass."""
    pytester.makeconftest(_PYTESTER_CONFTEST)
    pytester.makepyfile("""
        import subprocess
        import pytest

        @pytest.mark.cat
        def test_cat_dev_null():
            subprocess.run(["cat", "/dev/null"], check=True)
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_guards_work_with_xdist_workers(pytester: pytest.Pytester) -> None:
    """Guards enforce correctly when xdist distributes tests across workers.

    The controller creates wrapper scripts and sets PATH; workers inherit
    both via _PYTEST_GUARD_WRAPPER_DIR and enforce guards independently.
    Includes a marked test that passes and an unmarked test that calls the
    resource and should fail.
    """
    pytester.makeconftest(_PYTESTER_CONFTEST)
    pytester.makepyfile("""
        import subprocess
        import pytest

        @pytest.mark.cat
        def test_marked_cat():
            subprocess.run(["cat", "/dev/null"], check=True)

        def test_unmarked_cat_should_fail():
            subprocess.run(["cat", "/dev/null"], check=True)
    """)
    result = pytester.runpytest_subprocess("-n2", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(["*without @pytest.mark.cat*"])


def test_unmarked_test_that_calls_resource_fails(pytester: pytest.Pytester) -> None:
    """A test without the mark that calls cat should fail."""
    pytester.makeconftest(_PYTESTER_CONFTEST)
    pytester.makepyfile("""
        import subprocess

        def test_cat_dev_null():
            subprocess.run(["cat", "/dev/null"], check=True)
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*without @pytest.mark.cat*"])


def test_unmarked_test_that_handles_guard_error_still_fails(pytester: pytest.Pytester) -> None:
    """A test that expects a resource to fail should still be caught by the guard.

    This simulates a realistic scenario: a test checks that cat fails on a
    nonexistent file. The guard's exit 127 satisfies the assertion, so the
    test would silently pass without the blocked-invocation tracking.
    """
    pytester.makeconftest(_PYTESTER_CONFTEST)
    pytester.makepyfile("""
        import subprocess

        def test_cat_nonexistent_file():
            result = subprocess.run(
                ["cat", "/no/such/file"],
                capture_output=True,
            )
            assert result.returncode != 0
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*without @pytest.mark.cat*"])


def test_marked_test_that_never_calls_resource_fails(pytester: pytest.Pytester) -> None:
    """A test with @pytest.mark.cat that never calls cat should fail (superfluous mark)."""
    pytester.makeconftest(_PYTESTER_CONFTEST)
    pytester.makepyfile("""
        import pytest

        @pytest.mark.cat
        def test_never_calls_cat():
            assert 1 + 1 == 2
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*never invoked cat*"])


def test_blocked_resource_appended_to_failing_test(pytester: pytest.Pytester) -> None:
    """When a test fails AND a blocked resource was invoked, both should be visible."""
    pytester.makeconftest(_PYTESTER_CONFTEST)
    pytester.makepyfile("""
        import subprocess

        def test_fails_after_blocked_cat():
            subprocess.run(["cat", "/dev/null"], capture_output=True)
            assert False, "downstream failure"
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*downstream failure*",
            "*RESOURCE GUARD*without @pytest.mark.cat*",
        ]
    )


def test_unmarked_test_that_does_not_call_resource_passes(pytester: pytest.Pytester) -> None:
    """A test with no mark and no resource call should pass."""
    pytester.makeconftest(_PYTESTER_CONFTEST)
    pytester.makepyfile("""
        def test_no_cat():
            assert 1 + 1 == 2
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


def test_create_and_cleanup_round_trip(isolated_guard_state: None) -> None:
    """create_resource_guard_wrappers modifies PATH; cleanup restores it."""
    for resource in _TEST_RESOURCES:
        register_resource_guard(resource)
    create_resource_guard_wrappers()

    assert resource_guards._guard_wrapper_dir is not None
    wrapper_dir = resource_guards._guard_wrapper_dir
    assert os.environ["PATH"].startswith(wrapper_dir)

    for resource in _TEST_RESOURCES:
        assert (Path(wrapper_dir) / resource).exists()

    cleanup_resource_guard_wrappers()
    assert resource_guards._guard_wrapper_dir is None
    assert not Path(wrapper_dir).exists()
    assert not os.environ["PATH"].startswith(wrapper_dir)


def test_create_wrappers_generates_stub_for_missing_binary(
    isolated_guard_state: None,
) -> None:
    """A nonexistent binary gets a stub wrapper that exits 127."""
    register_resource_guard("nonexistent_xyz_binary")
    create_resource_guard_wrappers()

    wrapper_dir = resource_guards._guard_wrapper_dir
    assert wrapper_dir is not None
    stub = Path(wrapper_dir) / "nonexistent_xyz_binary"
    assert stub.exists()
    assert "not installed on this machine" in stub.read_text()

    cleanup_resource_guard_wrappers()


def test_create_wrappers_reuses_inherited_directory(
    isolated_guard_state: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When _PYTEST_GUARD_WRAPPER_DIR is set, wrappers are reused, not recreated."""
    monkeypatch.setenv("_PYTEST_GUARD_WRAPPER_DIR", str(tmp_path))

    create_resource_guard_wrappers()

    assert resource_guards._guard_wrapper_dir == str(tmp_path)
    assert resource_guards._owns_guard_wrapper_dir is False

    # Cleanup should not delete the directory since we don't own it
    cleanup_resource_guard_wrappers()
    assert tmp_path.exists()
    assert resource_guards._guard_wrapper_dir is None


def test_start_and_stop_resource_guards_round_trip(
    isolated_guard_state: None,
    request: pytest.FixtureRequest,
) -> None:
    """start_resource_guards creates wrappers, installs SDK guards, and registers hooks."""
    install_called = []
    cleanup_called = []
    register_resource_guard("echo")
    register_sdk_guard("test_sdk", lambda: install_called.append(1), lambda: cleanup_called.append(1))

    start_resource_guards(request.session)

    assert resource_guards._guard_wrapper_dir is not None
    assert install_called == [1]
    assert request.config.pluginmanager.get_plugin("resource_guards") is not None

    # Unregister the plugin before stop so it doesn't interfere with other tests
    request.config.pluginmanager.unregister(name="resource_guards")

    stop_resource_guards()

    assert resource_guards._guard_wrapper_dir is None
    assert cleanup_called == [1]


# ---------------------------------------------------------------------------
# SDK guard lifecycle (unit tests)
# ---------------------------------------------------------------------------


def test_register_sdk_guard_adds_entry(isolated_guard_state: None) -> None:
    install_called = []
    register_sdk_guard("test_sdk", lambda: install_called.append(1), lambda: None)

    assert len(resource_guards._registered_sdk_guards) == 1
    assert resource_guards._registered_sdk_guards[0][0] == "test_sdk"


def test_register_sdk_guard_deduplicates(isolated_guard_state: None) -> None:
    register_sdk_guard("test_sdk", lambda: None, lambda: None)
    register_sdk_guard("test_sdk", lambda: None, lambda: None)

    assert len(resource_guards._registered_sdk_guards) == 1


def test_create_sdk_resource_guards_installs_and_populates(
    isolated_guard_state: None,
) -> None:
    install_called = []
    register_sdk_guard("test_sdk", lambda: install_called.append(1), lambda: None)
    create_sdk_resource_guards()

    assert "test_sdk" in resource_guards._guarded_resources
    assert install_called == [1]


def test_cleanup_sdk_resource_guards_calls_cleanup(
    isolated_guard_state: None,
) -> None:
    cleanup_called = []
    register_sdk_guard("test_sdk", lambda: None, lambda: cleanup_called.append(1))
    cleanup_sdk_resource_guards()

    assert cleanup_called == [1]


def test_custom_sdk_guard_end_to_end(
    isolated_guard_state: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A custom SDK guard following the README pattern blocks, allows, and cleans up."""

    class FakeClient:
        def send(self, data: str) -> str:
            return f"sent:{data}"

    originals: dict[str, Callable[..., str]] = {}

    def guarded_send(self, data: str) -> str:
        enforce_sdk_guard("fake_sdk")
        return originals["send"](self, data)

    def install() -> None:
        originals["send"] = FakeClient.send
        FakeClient.send = guarded_send  # ty: ignore[invalid-assignment]

    def cleanup() -> None:
        if "send" in originals:
            FakeClient.send = originals["send"]  # ty: ignore[invalid-assignment]
            originals.clear()

    register_sdk_guard("fake_sdk", install, cleanup)
    create_sdk_resource_guards()

    # Blocked: calling send without the mark raises ResourceGuardViolation
    monkeypatch.setenv("_PYTEST_GUARD_PHASE", "call")
    monkeypatch.setenv("_PYTEST_GUARD_FAKE_SDK", "block")
    monkeypatch.setenv("_PYTEST_GUARD_TRACKING_DIR", str(tmp_path))

    with pytest.raises(ResourceGuardViolation, match="without @pytest.mark.fake_sdk"):
        FakeClient().send("hello")

    # Allowed: calling send with the mark works and tracks usage
    monkeypatch.setenv("_PYTEST_GUARD_FAKE_SDK", "allow")

    result = FakeClient().send("hello")
    assert result == "sent:hello"
    assert (tmp_path / "fake_sdk").exists()

    # Cleanup restores the original method
    cleanup_sdk_resource_guards()
    assert FakeClient().send("hello") == "sent:hello"
    assert len(originals) == 0


# ---------------------------------------------------------------------------
# create_sdk_method_guard (unit tests)
# ---------------------------------------------------------------------------


def test_create_sdk_method_guard_sync(
    isolated_guard_state: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """create_sdk_method_guard patches a sync method, enforces guard, and cleans up."""

    class Client:
        def call(self, x: int) -> int:
            return x * 2

    original_call = Client.call
    create_sdk_method_guard("test_sync", [(Client, "call", MethodKind.SYNC)])
    create_sdk_resource_guards()

    assert Client.call is not original_call

    # Guard blocks
    monkeypatch.setenv("_PYTEST_GUARD_PHASE", "call")
    monkeypatch.setenv("_PYTEST_GUARD_TEST_SYNC", "block")
    monkeypatch.setenv("_PYTEST_GUARD_TRACKING_DIR", str(tmp_path))
    with pytest.raises(ResourceGuardViolation):
        Client().call(5)

    # Guard allows
    monkeypatch.setenv("_PYTEST_GUARD_TEST_SYNC", "allow")
    assert Client().call(5) == 10

    # Cleanup restores
    cleanup_sdk_resource_guards()
    assert Client.call is original_call


def test_create_sdk_method_guard_async(
    isolated_guard_state: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """create_sdk_method_guard patches an async method, enforces guard, and cleans up."""

    class Client:
        async def call(self, x: int) -> int:
            return x * 2

    original_call = Client.call
    create_sdk_method_guard("test_async", [(Client, "call", MethodKind.ASYNC)])
    create_sdk_resource_guards()

    # Guard blocks
    monkeypatch.setenv("_PYTEST_GUARD_PHASE", "call")
    monkeypatch.setenv("_PYTEST_GUARD_TEST_ASYNC", "block")
    monkeypatch.setenv("_PYTEST_GUARD_TRACKING_DIR", str(tmp_path))
    with pytest.raises(ResourceGuardViolation):
        asyncio.get_event_loop().run_until_complete(Client().call(5))

    # Guard allows
    monkeypatch.setenv("_PYTEST_GUARD_TEST_ASYNC", "allow")
    result = asyncio.get_event_loop().run_until_complete(Client().call(5))
    assert result == 10

    # Cleanup restores
    cleanup_sdk_resource_guards()
    assert Client.call is original_call


def test_create_sdk_method_guard_async_gen(
    isolated_guard_state: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """create_sdk_method_guard patches an async generator method, enforces guard, and cleans up."""

    class Client:
        async def stream(self):
            yield 1
            yield 2

    original_stream = Client.stream
    create_sdk_method_guard("test_agen", [(Client, "stream", MethodKind.ASYNC_GEN)])
    create_sdk_resource_guards()

    # Guard blocks
    monkeypatch.setenv("_PYTEST_GUARD_PHASE", "call")
    monkeypatch.setenv("_PYTEST_GUARD_TEST_AGEN", "block")
    monkeypatch.setenv("_PYTEST_GUARD_TRACKING_DIR", str(tmp_path))

    async def collect_blocked():
        async for _item in Client().stream():
            pass

    with pytest.raises(ResourceGuardViolation):
        asyncio.get_event_loop().run_until_complete(collect_blocked())

    # Guard allows
    monkeypatch.setenv("_PYTEST_GUARD_TEST_AGEN", "allow")

    async def collect_allowed():
        results = []
        async for item in Client().stream():
            results.append(item)
        return results

    results = asyncio.get_event_loop().run_until_complete(collect_allowed())
    assert results == [1, 2]

    # Cleanup restores
    cleanup_sdk_resource_guards()
    assert Client.stream is original_stream


# ---------------------------------------------------------------------------
# _build_per_test_guard_env (unit tests)
# ---------------------------------------------------------------------------


def test_build_per_test_guard_env_sets_allow_for_marked_resources(
    isolated_guard_state: None,
) -> None:
    register_resource_guard("tmux")
    register_resource_guard("rsync")
    env = _build_per_test_guard_env({"tmux"}, "/tmp/track")

    assert env["_PYTEST_GUARD_PHASE"] == "call"
    assert env["_PYTEST_GUARD_TRACKING_DIR"] == "/tmp/track"
    assert env["_PYTEST_GUARD_TMUX"] == "allow"
    assert env["_PYTEST_GUARD_RSYNC"] == "block"


# ---------------------------------------------------------------------------
# _check_guard_violations (unit tests)
# ---------------------------------------------------------------------------


class _FakeReport:
    """Minimal stand-in for pytest.TestReport for testing _check_guard_violations."""

    def __init__(self, *, passed: bool, longrepr: str = "") -> None:
        self.outcome = "passed" if passed else "failed"
        self.longrepr = longrepr

    @property
    def passed(self) -> bool:
        return self.outcome == "passed"


def _make_state(tmp_path: Path, marks: set[str]) -> _PerTestGuardState:
    tracking_dir = str(tmp_path)
    return _PerTestGuardState(
        tracking_dir=tracking_dir,
        marks=marks,
        env_patcher=None,  # ty: ignore[invalid-type-form]
    )


def test_check_guard_violations_blocked_invocation_fails_passing_test(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """A passing test that invoked a blocked resource should be failed."""
    register_resource_guard("cat")
    (tmp_path / "blocked_cat").touch()

    state = _make_state(tmp_path, marks=set())
    report = _FakeReport(passed=True)
    _check_guard_violations(state, report)  # ty: ignore[invalid-argument-type]

    assert report.outcome == "failed"
    assert "without @pytest.mark.cat" in str(report.longrepr)


def test_check_guard_violations_blocked_invocation_appends_to_failing_test(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """A failing test that also invoked a blocked resource gets both messages."""
    register_resource_guard("cat")
    (tmp_path / "blocked_cat").touch()

    state = _make_state(tmp_path, marks=set())
    report = _FakeReport(passed=False, longrepr="original failure")
    _check_guard_violations(state, report)  # ty: ignore[invalid-argument-type]

    assert report.outcome == "failed"
    assert "original failure" in str(report.longrepr)
    assert "without @pytest.mark.cat" in str(report.longrepr)


def test_check_guard_violations_superfluous_mark_fails_test(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """A passing test marked with a resource it never invoked should be failed."""
    register_resource_guard("cat")

    state = _make_state(tmp_path, marks={"cat"})
    report = _FakeReport(passed=True)
    _check_guard_violations(state, report)  # ty: ignore[invalid-argument-type]

    assert report.outcome == "failed"
    assert "never invoked cat" in str(report.longrepr)


def test_check_guard_violations_no_violation_leaves_report_unchanged(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """A passing test that correctly used its marked resource should stay passed."""
    register_resource_guard("cat")
    (tmp_path / "cat").touch()

    state = _make_state(tmp_path, marks={"cat"})
    report = _FakeReport(passed=True)
    _check_guard_violations(state, report)  # ty: ignore[invalid-argument-type]

    assert report.outcome == "passed"


def test_check_guard_violations_skips_superfluous_check_on_failing_test(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """A failing test with a superfluous mark should not get the superfluous mark error."""
    register_resource_guard("cat")

    state = _make_state(tmp_path, marks={"cat"})
    report = _FakeReport(passed=False, longrepr="real failure")
    _check_guard_violations(state, report)  # ty: ignore[invalid-argument-type]

    assert report.outcome == "failed"
    assert "never invoked" not in str(report.longrepr)
    assert report.longrepr == "real failure"


# ---------------------------------------------------------------------------
# SDK guard: enforce_sdk_guard (unit tests)
# ---------------------------------------------------------------------------


def test_enforce_sdk_guard_blocks_when_unmarked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("_PYTEST_GUARD_PHASE", "call")
    monkeypatch.setenv("_PYTEST_GUARD_MYSDK", "block")
    monkeypatch.setenv("_PYTEST_GUARD_TRACKING_DIR", str(tmp_path))

    with pytest.raises(ResourceGuardViolation, match="without @pytest.mark.mysdk"):
        enforce_sdk_guard("mysdk")

    assert (tmp_path / "blocked_mysdk").exists()


def test_enforce_sdk_guard_allows_when_marked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("_PYTEST_GUARD_PHASE", "call")
    monkeypatch.setenv("_PYTEST_GUARD_MYSDK", "allow")
    monkeypatch.setenv("_PYTEST_GUARD_TRACKING_DIR", str(tmp_path))

    enforce_sdk_guard("mysdk")

    assert (tmp_path / "mysdk").exists()


def test_enforce_sdk_guard_skips_outside_call_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("_PYTEST_GUARD_PHASE", "setup")
    monkeypatch.setenv("_PYTEST_GUARD_MYSDK", "block")
    monkeypatch.setenv("_PYTEST_GUARD_TRACKING_DIR", str(tmp_path))

    enforce_sdk_guard("mysdk")

    assert not (tmp_path / "blocked_mysdk").exists()
    assert not (tmp_path / "mysdk").exists()


def test_enforce_sdk_guard_skips_when_no_phase_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("_PYTEST_GUARD_PHASE", raising=False)
    monkeypatch.setenv("_PYTEST_GUARD_MYSDK", "block")
    monkeypatch.setenv("_PYTEST_GUARD_TRACKING_DIR", str(tmp_path))

    enforce_sdk_guard("mysdk")

    assert not (tmp_path / "blocked_mysdk").exists()


# ---------------------------------------------------------------------------
# SDK guard: end-to-end behavior (pytester)
# ---------------------------------------------------------------------------

# Conftest for SDK guard pytester tests. Registers a no-op SDK guard, then uses
# start/stop_resource_guards to initialize the infrastructure. Tests trigger the
# guard by calling enforce_sdk_guard directly (no real SDK needed).
_PYTESTER_SDK_CONFTEST = """\
import os
from imbue.resource_guards.resource_guards import (
    register_sdk_guard,
    start_resource_guards,
    stop_resource_guards,
)

# Clear inherited guard state so we create fresh wrappers.
os.environ.pop("_PYTEST_GUARD_WRAPPER_DIR", None)

def pytest_configure(config):
    config.addinivalue_line("markers", "test_sdk: test uses test_sdk")

register_sdk_guard("test_sdk", lambda: None, lambda: None)

def pytest_sessionstart(session):
    start_resource_guards(session)

def pytest_sessionfinish(session, exitstatus):
    stop_resource_guards()
"""


def test_sdk_marked_test_that_triggers_guard_passes(pytester: pytest.Pytester) -> None:
    """A test with the SDK mark that triggers the guard should pass."""
    pytester.makeconftest(_PYTESTER_SDK_CONFTEST)
    pytester.makepyfile("""
        import pytest
        from imbue.resource_guards.resource_guards import enforce_sdk_guard

        @pytest.mark.test_sdk
        def test_sdk_call():
            enforce_sdk_guard("test_sdk")
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_sdk_unmarked_test_that_triggers_guard_fails(pytester: pytest.Pytester) -> None:
    """A test without the SDK mark that triggers the guard should fail."""
    pytester.makeconftest(_PYTESTER_SDK_CONFTEST)
    pytester.makepyfile("""
        from imbue.resource_guards.resource_guards import enforce_sdk_guard

        def test_sdk_call():
            enforce_sdk_guard("test_sdk")
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*without @pytest.mark.test_sdk*"])


def test_sdk_unmarked_test_that_catches_guard_error_still_fails(
    pytester: pytest.Pytester,
) -> None:
    """A test that catches ResourceGuardViolation should still be caught by the guard.

    The blocked tracking file ensures makereport fails the test even when the
    exception is swallowed, mirroring the binary guard's exit-127 tracking.
    """
    pytester.makeconftest(_PYTESTER_SDK_CONFTEST)
    pytester.makepyfile("""
        from imbue.resource_guards.resource_guards import ResourceGuardViolation
        from imbue.resource_guards.resource_guards import enforce_sdk_guard

        def test_sdk_catches_error():
            try:
                enforce_sdk_guard("test_sdk")
            except ResourceGuardViolation:
                pass
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*without @pytest.mark.test_sdk*"])


def test_sdk_marked_test_that_never_triggers_guard_fails(
    pytester: pytest.Pytester,
) -> None:
    """A test with the SDK mark that never triggers the guard fails (superfluous mark)."""
    pytester.makeconftest(_PYTESTER_SDK_CONFTEST)
    pytester.makepyfile("""
        import pytest

        @pytest.mark.test_sdk
        def test_never_calls_sdk():
            assert 1 + 1 == 2
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*never invoked test_sdk*"])


def test_sdk_unmarked_test_that_does_not_trigger_guard_passes(
    pytester: pytest.Pytester,
) -> None:
    """A test with no SDK mark and no guard trigger should pass."""
    pytester.makeconftest(_PYTESTER_SDK_CONFTEST)
    pytester.makepyfile("""
        def test_no_sdk():
            assert 1 + 1 == 2
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
