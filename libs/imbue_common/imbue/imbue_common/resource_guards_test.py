import os
from pathlib import Path

import pytest

import imbue.imbue_common.resource_guards as rg
from imbue.imbue_common.resource_guards import ResourceGuardViolation
from imbue.imbue_common.resource_guards import cleanup_resource_guard_wrappers
from imbue.imbue_common.resource_guards import create_resource_guard_wrappers
from imbue.imbue_common.resource_guards import enforce_sdk_guard
from imbue.imbue_common.resource_guards import generate_wrapper_script
from imbue.imbue_common.resource_guards import register_resource_guard

# Use ubiquitous coreutils binaries so these tests run on any system.
_TEST_RESOURCES = ["echo", "cat", "ls"]

# Conftest that pytester injects into its temp directory.  It registers the
# resource guard hooks for "cat" only, which is enough for end-to-end tests.
# cat is a good choice: `cat /dev/null` succeeds, `cat /nonexistent` fails.
_PYTESTER_CONFTEST = """\
import os
import pytest
from imbue.imbue_common.resource_guards import (
    register_resource_guard,
    create_resource_guard_wrappers,
    cleanup_resource_guard_wrappers,
    _pytest_runtest_setup,
    _pytest_runtest_teardown,
    _pytest_runtest_makereport,
)

# Clear inherited guard state so we create fresh wrappers for our resources.
os.environ.pop("_PYTEST_GUARD_WRAPPER_DIR", None)

register_resource_guard("cat")

def pytest_configure(config):
    config.addinivalue_line("markers", "cat: test uses cat")

def pytest_sessionstart(session):
    create_resource_guard_wrappers()

def pytest_sessionfinish(session, exitstatus):
    cleanup_resource_guard_wrappers()

pytest_runtest_setup = _pytest_runtest_setup
pytest_runtest_teardown = _pytest_runtest_teardown
pytest_runtest_makereport = _pytest_runtest_makereport
"""

pytest_plugins = ["pytester"]


@pytest.fixture()
def isolated_guard_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate resource guard module state so create/cleanup don't affect the session."""
    monkeypatch.setattr(rg, "_guard_wrapper_dir", None)
    monkeypatch.setattr(rg, "_owns_guard_wrapper_dir", False)
    monkeypatch.setattr(rg, "_session_env_patcher", None)
    monkeypatch.setattr(rg, "_guarded_resources", [])
    monkeypatch.setattr(rg, "_registered_sdk_guards", [])
    monkeypatch.delenv("_PYTEST_GUARD_WRAPPER_DIR", raising=False)


# ---------------------------------------------------------------------------
# Script generation (unit tests)
# ---------------------------------------------------------------------------


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

    assert rg._guard_wrapper_dir is not None
    wrapper_dir = rg._guard_wrapper_dir
    assert os.environ["PATH"].startswith(wrapper_dir)

    for resource in _TEST_RESOURCES:
        assert (Path(wrapper_dir) / resource).exists()

    cleanup_resource_guard_wrappers()
    assert rg._guard_wrapper_dir is None
    assert not Path(wrapper_dir).exists()
    assert not os.environ["PATH"].startswith(wrapper_dir)


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

# Conftest for SDK guard pytester tests. Uses create_resource_guard_wrappers()
# to initialize the guard infrastructure, then adds an SDK resource. Tests trigger
# the guard by calling enforce_sdk_guard directly (no real SDK needed).
_PYTESTER_SDK_CONFTEST = """\
import os
import pytest
from imbue.imbue_common.resource_guards import (
    create_resource_guard_wrappers,
    cleanup_resource_guard_wrappers,
    register_sdk_guard,
    create_sdk_resource_guards,
    cleanup_sdk_resource_guards,
    _pytest_runtest_setup,
    _pytest_runtest_teardown,
    _pytest_runtest_makereport,
)

# Clear inherited guard state so we create fresh wrappers.
os.environ.pop("_PYTEST_GUARD_WRAPPER_DIR", None)

def pytest_configure(config):
    config.addinivalue_line("markers", "test_sdk: test uses test_sdk")

register_sdk_guard("test_sdk", lambda: None, lambda: None)

def pytest_sessionstart(session):
    create_resource_guard_wrappers()
    create_sdk_resource_guards()

def pytest_sessionfinish(session, exitstatus):
    cleanup_sdk_resource_guards()
    cleanup_resource_guard_wrappers()

pytest_runtest_setup = _pytest_runtest_setup
pytest_runtest_teardown = _pytest_runtest_teardown
pytest_runtest_makereport = _pytest_runtest_makereport
"""


def test_sdk_marked_test_that_triggers_guard_passes(pytester: pytest.Pytester) -> None:
    """A test with the SDK mark that triggers the guard should pass."""
    pytester.makeconftest(_PYTESTER_SDK_CONFTEST)
    pytester.makepyfile("""
        import pytest
        from imbue.imbue_common.resource_guards import enforce_sdk_guard

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
        from imbue.imbue_common.resource_guards import enforce_sdk_guard

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
        from imbue.imbue_common.resource_guards import ResourceGuardViolation
        from imbue.imbue_common.resource_guards import enforce_sdk_guard

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
