import asyncio
import os
from collections.abc import Callable
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

import imbue.resource_guards.resource_guards as resource_guards
from imbue.resource_guards.resource_guards import MethodKind
from imbue.resource_guards.resource_guards import ResourceGuardMisconfiguration
from imbue.resource_guards.resource_guards import ResourceGuardViolation
from imbue.resource_guards.resource_guards import _GuardViolation
from imbue.resource_guards.resource_guards import _GuardViolationKind
from imbue.resource_guards.resource_guards import _PerTestGuardState
from imbue.resource_guards.resource_guards import _build_guard_env
from imbue.resource_guards.resource_guards import _check_fixture_at_scope_end
from imbue.resource_guards.resource_guards import _check_fixture_blocked_after_setup
from imbue.resource_guards.resource_guards import _check_guard_violations
from imbue.resource_guards.resource_guards import _collect_fixture_covered_resources
from imbue.resource_guards.resource_guards import _detect_guard_violations
from imbue.resource_guards.resource_guards import _make_guarded_fixture_wrapper
from imbue.resource_guards.resource_guards import _pytest_fixture_setup
from imbue.resource_guards.resource_guards import _pytest_runtest_setup
from imbue.resource_guards.resource_guards import cleanup_resource_guard_wrappers
from imbue.resource_guards.resource_guards import cleanup_sdk_resource_guards
from imbue.resource_guards.resource_guards import create_resource_guard_wrappers
from imbue.resource_guards.resource_guards import create_sdk_method_guard
from imbue.resource_guards.resource_guards import create_sdk_resource_guards
from imbue.resource_guards.resource_guards import enforce_sdk_guard
from imbue.resource_guards.resource_guards import fixture_uses_resources
from imbue.resource_guards.resource_guards import generate_stub_wrapper_script
from imbue.resource_guards.resource_guards import generate_wrapper_script
from imbue.resource_guards.resource_guards import get_guarded_resource_names
from imbue.resource_guards.resource_guards import register_all_resource_guards
from imbue.resource_guards.resource_guards import register_guarded_resource_markers
from imbue.resource_guards.resource_guards import register_resource_guard
from imbue.resource_guards.resource_guards import register_sdk_guard
from imbue.resource_guards.resource_guards import start_resource_guards
from imbue.resource_guards.resource_guards import stop_resource_guards

# Use ubiquitous coreutils binaries so these tests run on any system.
_TEST_RESOURCES = ["echo", "cat", "ls"]

# Conftest that pytester injects into its temp directory.  It registers the
# resource guard hooks for "cat" only, which is enough for end-to-end tests.
# cat is a good choice: `cat /dev/null` succeeds, `cat /nonexistent` fails.
# Tests using pytester must request the clean_guard_env fixture so the
# child subprocess doesn't inherit the outer process's guard wrapper PATH.
_PYTESTER_CONFTEST = """\
from imbue.resource_guards.resource_guards import (
    register_resource_guard,
    start_resource_guards,
    stop_resource_guards,
)

register_resource_guard("cat")

def pytest_configure(config):
    config.addinivalue_line("markers", "cat: test uses cat")

def pytest_sessionstart(session):
    start_resource_guards(session)

def pytest_sessionfinish(session, exitstatus):
    stop_resource_guards()
"""

# Variant of the pytester conftest that registers two guards. Used by the
# multi-resource fixture tests; both cat and ls are guarded so a single
# fixture can declare both via @fixture_uses_resources("cat", "ls").
_PYTESTER_CONFTEST_TWO_GUARDS = """\
from imbue.resource_guards.resource_guards import (
    register_resource_guard,
    start_resource_guards,
    stop_resource_guards,
)

register_resource_guard("cat")
register_resource_guard("ls")

def pytest_configure(config):
    config.addinivalue_line("markers", "cat: test uses cat")
    config.addinivalue_line("markers", "ls: test uses ls")

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


def test_marked_test_that_calls_resource_passes(pytester: pytest.Pytester, clean_guard_env: None) -> None:
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


def test_guards_work_with_xdist_workers(pytester: pytest.Pytester, clean_guard_env: None) -> None:
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


def test_unmarked_test_that_calls_resource_fails(pytester: pytest.Pytester, clean_guard_env: None) -> None:
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


def test_unmarked_test_that_handles_guard_error_still_fails(pytester: pytest.Pytester, clean_guard_env: None) -> None:
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


def test_marked_test_that_never_calls_resource_fails(pytester: pytest.Pytester, clean_guard_env: None) -> None:
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


@pytest.mark.timeout(30)
def test_blocked_resource_appended_to_failing_test(pytester: pytest.Pytester, clean_guard_env: None) -> None:
    """When a test fails AND a blocked resource was invoked, both should be visible.

    Spawns a pytest subprocess via ``runpytest_subprocess``, whose startup is slow and
    variable under offload load and intermittently exceeds the default 10s timeout.
    """
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


def test_unmarked_test_that_does_not_call_resource_passes(pytester: pytest.Pytester, clean_guard_env: None) -> None:
    """A test with no mark and no resource call should pass."""
    pytester.makeconftest(_PYTESTER_CONFTEST)
    pytester.makepyfile("""
        def test_no_cat():
            assert 1 + 1 == 2
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


# ---------------------------------------------------------------------------
# Fixture-scope guard (@fixture_uses_resources)
# ---------------------------------------------------------------------------


def test_fixture_declaring_resource_authorizes_setup_calls(pytester: pytest.Pytester, clean_guard_env: None) -> None:
    """A tagged module-scoped fixture's setup calls are authorized against its own declaration.

    The consuming test must carry @pytest.mark.cat (since the fixture declares cat),
    but no longer needs to invoke cat directly -- the fixture's setup-time call
    satisfies the mark transitively.
    """
    pytester.makeconftest(_PYTESTER_CONFTEST)
    pytester.makepyfile("""
        import subprocess
        import pytest

        from imbue.resource_guards.resource_guards import fixture_uses_resources

        @pytest.fixture(scope="module")
        @fixture_uses_resources("cat")
        def cat_fixture():
            subprocess.run(["cat", "/dev/null"], check=True)
            yield "value"

        @pytest.mark.cat
        def test_consumer_with_mark(cat_fixture):
            assert cat_fixture == "value"
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_unmarked_consumer_of_tagged_fixture_fails(pytester: pytest.Pytester, clean_guard_env: None) -> None:
    """Consuming a @fixture_uses_resources fixture without the matching mark fails the test.

    The mark is required so that `pytest -m <resource>` reliably selects every
    test that transitively needs the resource. If you consume a tagged fixture,
    you must declare the dependency on the consuming test as well.
    """
    pytester.makeconftest(_PYTESTER_CONFTEST)
    pytester.makepyfile("""
        import subprocess
        import pytest

        from imbue.resource_guards.resource_guards import fixture_uses_resources

        @pytest.fixture
        @fixture_uses_resources("cat")
        def cat_fixture():
            subprocess.run(["cat", "/dev/null"], check=True)
            yield "value"

        def test_consumer_without_mark(cat_fixture):
            assert cat_fixture == "value"
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*missing @pytest.mark.cat*"])


def test_fixture_declares_resource_but_does_not_use_it_fails(pytester: pytest.Pytester, clean_guard_env: None) -> None:
    """A fixture that declares a resource it never invokes (in setup or teardown) fires at scope end.

    The check is deferred to a fixture-scope finalizer, so the consuming
    test's call phase passes; the violation surfaces as a teardown error.
    """
    pytester.makeconftest(_PYTESTER_CONFTEST)
    pytester.makepyfile("""
        import pytest

        from imbue.resource_guards.resource_guards import fixture_uses_resources

        @pytest.fixture
        @fixture_uses_resources("cat")
        def empty_fixture():
            yield "value"

        @pytest.mark.cat
        def test_consumer(empty_fixture):
            assert empty_fixture == "value"
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    # The call phase passes (the fixture had no BLOCKED issue, and the
    # consumer doesn't actually run cat). The fixture-scope finalizer then
    # fires NEVER_INVOKED -- pytest reports that as a teardown error.
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*did not invoke cat during setup or teardown*"])


def test_fixture_uses_resource_without_declaring_it_fails(pytester: pytest.Pytester, clean_guard_env: None) -> None:
    """A fixture that uses cat without declaring it should fail at setup."""
    pytester.makeconftest(_PYTESTER_CONFTEST)
    pytester.makepyfile("""
        import subprocess
        import pytest

        from imbue.resource_guards.resource_guards import fixture_uses_resources

        @pytest.fixture
        @fixture_uses_resources("ls")
        def wrong_fixture():
            # Declares "ls" but actually calls cat. Cat is not in the fixture's
            # declared resources, so the call should be blocked.
            subprocess.run(["cat", "/dev/null"], capture_output=True)
            yield "value"

        def test_consumer(wrong_fixture):
            assert wrong_fixture == "value"
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*did not declare it*"])


def test_marked_consumer_of_tagged_fixture_passes_without_direct_use(
    pytester: pytest.Pytester, clean_guard_env: None
) -> None:
    """A test may carry @pytest.mark.<resource> when a tagged fixture covers it.

    The fixture's @fixture_uses_resources("cat") declaration is independently
    verified to actually invoke cat during setup, so @pytest.mark.cat on a
    consuming test is meaningful even when the test body never calls cat
    directly. This lets `pytest -m cat` select all tests that transitively
    need cat without the mark becoming a NEVER_INVOKED violation.
    """
    pytester.makeconftest(_PYTESTER_CONFTEST)
    pytester.makepyfile("""
        import subprocess
        import pytest

        from imbue.resource_guards.resource_guards import fixture_uses_resources

        @pytest.fixture
        @fixture_uses_resources("cat")
        def cat_fixture():
            subprocess.run(["cat", "/dev/null"], check=True)
            yield "value"

        @pytest.mark.cat
        def test_marked_but_body_does_not_use_cat(cat_fixture):
            assert cat_fixture == "value"
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_marked_consumer_of_tagged_fixture_may_also_invoke_resource_directly(
    pytester: pytest.Pytester, clean_guard_env: None
) -> None:
    """A consumer with the mark may both consume a tagged fixture and call the resource directly.

    The mark authorizes the test body's direct invocation (block check),
    and the fixture's declaration covers the transitive use; the test
    passing satisfies both reasons simultaneously without conflict.
    """
    pytester.makeconftest(_PYTESTER_CONFTEST)
    pytester.makepyfile("""
        import subprocess
        import pytest

        from imbue.resource_guards.resource_guards import fixture_uses_resources

        @pytest.fixture
        @fixture_uses_resources("cat")
        def cat_fixture():
            subprocess.run(["cat", "/dev/null"], check=True)
            yield "value"

        @pytest.mark.cat
        def test_consumer_also_uses_cat_directly(cat_fixture):
            subprocess.run(["cat", "/dev/null"], check=True)
            assert cat_fixture == "value"
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_fixture_teardown_resource_calls_authorized_against_fixture(
    pytester: pytest.Pytester, clean_guard_env: None
) -> None:
    """Fixture teardown calls should also run under the fixture's guard scope.

    Teardown happens during the last consuming test's lifecycle; its resource
    calls must be authorized against the fixture's declaration, not the test's
    env. Both consumers carry the mark (required because they consume a
    tagged fixture).
    """
    pytester.makeconftest(_PYTESTER_CONFTEST)
    pytester.makepyfile("""
        import subprocess
        import pytest

        from imbue.resource_guards.resource_guards import fixture_uses_resources

        @pytest.fixture(scope="module")
        @fixture_uses_resources("cat")
        def cat_fixture():
            subprocess.run(["cat", "/dev/null"], check=True)
            yield "value"
            # Teardown invocation; should not be blocked even when the
            # last consuming test lacks any direct cat usage.
            subprocess.run(["cat", "/dev/null"], check=True)

        @pytest.mark.cat
        def test_first_consumer(cat_fixture):
            assert cat_fixture == "value"

        @pytest.mark.cat
        def test_second_consumer(cat_fixture):
            assert cat_fixture == "value"
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)


def test_fixture_invoking_resource_only_during_teardown_passes(
    pytester: pytest.Pytester, clean_guard_env: None
) -> None:
    """A fixture that only uses its declared resource during teardown still satisfies the check.

    The NEVER_INVOKED check is deferred to a fixture-scope finalizer that
    runs after the wrapper's post-yield body, so teardown-only invocations
    count toward satisfying the declaration.
    """
    pytester.makeconftest(_PYTESTER_CONFTEST)
    pytester.makepyfile("""
        import subprocess
        import pytest

        from imbue.resource_guards.resource_guards import fixture_uses_resources

        @pytest.fixture
        @fixture_uses_resources("cat")
        def teardown_only_fixture():
            # Setup invokes nothing.
            yield "value"
            # Teardown invokes cat.
            subprocess.run(["cat", "/dev/null"], check=True)

        @pytest.mark.cat
        def test_consumer(teardown_only_fixture):
            assert teardown_only_fixture == "value"
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_fixture_with_undeclared_blocked_invocation_in_teardown_fails(
    pytester: pytest.Pytester, clean_guard_env: None
) -> None:
    """Teardown-phase BLOCKED is caught by the scope-end check when the resource is guarded."""
    pytester.makeconftest(_PYTESTER_CONFTEST_TWO_GUARDS)
    pytester.makepyfile("""
        import subprocess
        import pytest

        from imbue.resource_guards.resource_guards import fixture_uses_resources

        @pytest.fixture
        @fixture_uses_resources("cat")
        def fixture():
            # Setup invokes cat (declared); no BLOCKED here, no NEVER_INVOKED.
            subprocess.run(["cat", "/dev/null"], check=True)
            yield "value"
            # Teardown invokes ls, which is NOT in the fixture's declared
            # resources. The wrapper blocks it (exits 127) and writes
            # blocked_ls. The setup-time BLOCKED check has already passed;
            # the at-scope-end check picks this up.
            subprocess.run(["ls", "/"], capture_output=True)

        @pytest.mark.cat
        def test_consumer(fixture):
            assert fixture == "value"
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    # Call passes; the scope-end finalizer raises during fixture teardown.
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*invoked 'ls' but did not declare it*"])


def test_fixture_with_multiple_declared_resources_split_across_setup_and_teardown_passes(
    pytester: pytest.Pytester, clean_guard_env: None
) -> None:
    """A fixture declaring two resources may invoke one in setup and the other in teardown."""
    pytester.makeconftest(_PYTESTER_CONFTEST_TWO_GUARDS)
    pytester.makepyfile("""
        import subprocess
        import pytest

        from imbue.resource_guards.resource_guards import fixture_uses_resources

        @pytest.fixture
        @fixture_uses_resources("cat", "ls")
        def split_fixture():
            subprocess.run(["cat", "/dev/null"], check=True)
            yield "value"
            subprocess.run(["ls", "/"], check=True, capture_output=True)

        @pytest.mark.cat
        @pytest.mark.ls
        def test_consumer(split_fixture):
            assert split_fixture == "value"
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_fixture_declaring_two_resources_invoking_only_one_in_teardown_fails(
    pytester: pytest.Pytester, clean_guard_env: None
) -> None:
    """If only one of two declared resources is ever invoked (in setup or teardown), the other fires."""
    pytester.makeconftest(_PYTESTER_CONFTEST_TWO_GUARDS)
    pytester.makepyfile("""
        import subprocess
        import pytest

        from imbue.resource_guards.resource_guards import fixture_uses_resources

        @pytest.fixture
        @fixture_uses_resources("cat", "ls")
        def partial_fixture():
            yield "value"
            # Only teardown uses cat; ls is never invoked.
            subprocess.run(["cat", "/dev/null"], check=True)

        @pytest.mark.cat
        @pytest.mark.ls
        def test_consumer(partial_fixture):
            assert partial_fixture == "value"
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*did not invoke ls during setup or teardown*"])


def test_fixture_setup_blocked_invocation_surfaces_at_setup_phase(
    pytester: pytest.Pytester, clean_guard_env: None
) -> None:
    """A BLOCKED invocation during setup surfaces immediately as a setup-phase error.

    The deferred at-scope-end check must NOT redundantly fire on the same
    blocked tracking file -- otherwise the user would see the same error
    twice (once at setup, once at scope-end). The inline check sets the
    setup_failed flag so the deferred check skips.
    """
    pytester.makeconftest(_PYTESTER_CONFTEST_TWO_GUARDS)
    pytester.makepyfile("""
        import subprocess
        import pytest

        from imbue.resource_guards.resource_guards import fixture_uses_resources

        @pytest.fixture
        @fixture_uses_resources("ls")
        def fixture():
            # Setup invokes cat, which is NOT declared -- BLOCKED fires
            # in the inline check after yield.
            subprocess.run(["cat", "/dev/null"], capture_output=True)
            yield "value"

        @pytest.mark.ls
        def test_consumer(fixture):
            assert fixture == "value"
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    # Exactly one error -- the inline BLOCKED check. The deferred check
    # should skip because setup_failed was set.
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*invoked 'cat' but did not declare it*"])


def test_fixture_declaring_multiple_resources_authorizes_each(
    pytester: pytest.Pytester, clean_guard_env: None
) -> None:
    """A fixture declaring two resources may invoke either during setup; consumer needs both marks."""
    pytester.makeconftest(_PYTESTER_CONFTEST_TWO_GUARDS)
    pytester.makepyfile("""
        import subprocess
        import pytest

        from imbue.resource_guards.resource_guards import fixture_uses_resources

        @pytest.fixture
        @fixture_uses_resources("cat", "ls")
        def multi_fixture():
            subprocess.run(["cat", "/dev/null"], check=True)
            subprocess.run(["ls", "/"], check=True, capture_output=True)
            yield "value"

        @pytest.mark.cat
        @pytest.mark.ls
        def test_consumer_with_both_marks(multi_fixture):
            assert multi_fixture == "value"
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_consumer_missing_one_of_multiple_fixture_marks_fails(
    pytester: pytest.Pytester, clean_guard_env: None
) -> None:
    """When a fixture declares two resources, the consumer must carry both marks."""
    pytester.makeconftest(_PYTESTER_CONFTEST_TWO_GUARDS)
    pytester.makepyfile("""
        import subprocess
        import pytest

        from imbue.resource_guards.resource_guards import fixture_uses_resources

        @pytest.fixture
        @fixture_uses_resources("cat", "ls")
        def multi_fixture():
            subprocess.run(["cat", "/dev/null"], check=True)
            subprocess.run(["ls", "/"], check=True, capture_output=True)
            yield "value"

        @pytest.mark.cat
        def test_consumer_missing_ls_mark(multi_fixture):
            assert multi_fixture == "value"
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*missing @pytest.mark.ls*"])


def test_multi_resource_fixture_must_invoke_each_declared_resource(
    pytester: pytest.Pytester, clean_guard_env: None
) -> None:
    """A fixture declaring two resources but invoking only one fails the NEVER_INVOKED check on the other."""
    pytester.makeconftest(_PYTESTER_CONFTEST_TWO_GUARDS)
    pytester.makepyfile("""
        import subprocess
        import pytest

        from imbue.resource_guards.resource_guards import fixture_uses_resources

        @pytest.fixture
        @fixture_uses_resources("cat", "ls")
        def half_used_fixture():
            subprocess.run(["cat", "/dev/null"], check=True)
            # Never invokes ls -- the fixture-scope check should catch this.
            yield "value"

        @pytest.mark.cat
        @pytest.mark.ls
        def test_consumer(half_used_fixture):
            assert half_used_fixture == "value"
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    # The consumer's call phase passes; the fixture-scope finalizer raises
    # NEVER_INVOKED for ls at teardown.
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*did not invoke ls during setup or teardown*"])


def test_consumer_of_two_distinct_tagged_fixtures_requires_each_mark(
    pytester: pytest.Pytester, clean_guard_env: None
) -> None:
    """A test consuming two fixtures, each tagged for a different resource, requires both marks."""
    pytester.makeconftest(_PYTESTER_CONFTEST_TWO_GUARDS)
    pytester.makepyfile("""
        import subprocess
        import pytest

        from imbue.resource_guards.resource_guards import fixture_uses_resources

        @pytest.fixture
        @fixture_uses_resources("cat")
        def cat_fixture():
            subprocess.run(["cat", "/dev/null"], check=True)
            yield "cat"

        @pytest.fixture
        @fixture_uses_resources("ls")
        def ls_fixture():
            subprocess.run(["ls", "/"], check=True, capture_output=True)
            yield "ls"

        @pytest.mark.cat
        @pytest.mark.ls
        def test_consumes_both(cat_fixture, ls_fixture):
            assert (cat_fixture, ls_fixture) == ("cat", "ls")
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_consumer_of_two_distinct_tagged_fixtures_missing_one_mark_fails(
    pytester: pytest.Pytester, clean_guard_env: None
) -> None:
    """Each tagged fixture in the closure independently requires its mark on the consumer."""
    pytester.makeconftest(_PYTESTER_CONFTEST_TWO_GUARDS)
    pytester.makepyfile("""
        import subprocess
        import pytest

        from imbue.resource_guards.resource_guards import fixture_uses_resources

        @pytest.fixture
        @fixture_uses_resources("cat")
        def cat_fixture():
            subprocess.run(["cat", "/dev/null"], check=True)
            yield "cat"

        @pytest.fixture
        @fixture_uses_resources("ls")
        def ls_fixture():
            subprocess.run(["ls", "/"], check=True, capture_output=True)
            yield "ls"

        @pytest.mark.cat
        def test_only_marks_cat(cat_fixture, ls_fixture):
            assert (cat_fixture, ls_fixture) == ("cat", "ls")
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*missing @pytest.mark.ls*"])


def test_nested_tagged_fixtures_contribute_their_resources_to_consumer(
    pytester: pytest.Pytester, clean_guard_env: None
) -> None:
    """A tagged fixture that depends on another tagged fixture surfaces both resources in the closure.

    Pytest's static closure for the consumer includes every fixture in
    the dependency chain, so the helper picks up both decorations and
    the consumer must carry marks for each.
    """
    pytester.makeconftest(_PYTESTER_CONFTEST_TWO_GUARDS)
    pytester.makepyfile("""
        import subprocess
        import pytest

        from imbue.resource_guards.resource_guards import fixture_uses_resources

        @pytest.fixture
        @fixture_uses_resources("cat")
        def inner_cat_fixture():
            subprocess.run(["cat", "/dev/null"], check=True)
            yield "cat"

        @pytest.fixture
        @fixture_uses_resources("ls")
        def outer_ls_fixture(inner_cat_fixture):
            subprocess.run(["ls", "/"], check=True, capture_output=True)
            yield (inner_cat_fixture, "ls")

        @pytest.mark.cat
        @pytest.mark.ls
        def test_consumes_outer(outer_ls_fixture):
            assert outer_ls_fixture == ("cat", "ls")
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_tagged_fixture_in_parent_conftest_used_by_subdir_test(
    pytester: pytest.Pytester, clean_guard_env: None
) -> None:
    """A tagged fixture defined in a parent conftest must reach tests in a subdirectory."""
    pytester.makeconftest("""
        import subprocess
        import pytest

        from imbue.resource_guards.resource_guards import (
            fixture_uses_resources,
            register_resource_guard,
            start_resource_guards,
            stop_resource_guards,
        )

        register_resource_guard("cat")

        def pytest_configure(config):
            config.addinivalue_line("markers", "cat: test uses cat")

        def pytest_sessionstart(session):
            start_resource_guards(session)

        def pytest_sessionfinish(session, exitstatus):
            stop_resource_guards()

        @pytest.fixture
        @fixture_uses_resources("cat")
        def parent_cat_fixture():
            subprocess.run(["cat", "/dev/null"], check=True)
            yield "value"
    """)
    pytester.makepyfile(
        **{
            "subdir/test_subdir": """
                import pytest

                @pytest.mark.cat
                def test_consumes_parent_fixture(parent_cat_fixture):
                    assert parent_cat_fixture == "value"
            """,
        }
    )
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_parametrized_tagged_fixture_runs_for_each_param(pytester: pytest.Pytester, clean_guard_env: None) -> None:
    """Parametrization on a tagged fixture should not trigger the multi-FixtureDef override error."""
    pytester.makeconftest(_PYTESTER_CONFTEST)
    pytester.makepyfile("""
        import subprocess
        import pytest

        from imbue.resource_guards.resource_guards import fixture_uses_resources

        @pytest.fixture(params=["a", "b"])
        @fixture_uses_resources("cat")
        def param_cat_fixture(request):
            subprocess.run(["cat", "/dev/null"], check=True)
            yield request.param

        @pytest.mark.cat
        def test_uses_param_fixture(param_cat_fixture):
            assert param_cat_fixture in ("a", "b")
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)


def test_tagged_fixture_override_reports_clean_error_end_to_end(
    pytester: pytest.Pytester, clean_guard_env: None
) -> None:
    """An override of a tagged fixture surfaces the ResourceGuardMisconfiguration cleanly.

    Regression test: _collect_fixture_covered_resources runs during
    _pytest_runtest_setup. When it raises (tagged-fixture override case), the
    per-test guard state must still be initialized so that teardown and
    makereport hooks do not crash with AttributeError -- otherwise the
    original ResourceGuardMisconfiguration would be buried under a cascading
    AttributeError and the user would see a confusing trace instead of the
    "multiple definitions" message.
    """
    # Root conftest registers the guard and defines a tagged fixture.
    pytester.makeconftest("""
        import subprocess
        import pytest

        from imbue.resource_guards.resource_guards import (
            fixture_uses_resources,
            register_resource_guard,
            start_resource_guards,
            stop_resource_guards,
        )

        register_resource_guard("cat")

        def pytest_configure(config):
            config.addinivalue_line("markers", "cat: test uses cat")

        def pytest_sessionstart(session):
            start_resource_guards(session)

        def pytest_sessionfinish(session, exitstatus):
            stop_resource_guards()

        @pytest.fixture
        @fixture_uses_resources("cat")
        def shared_fixture():
            subprocess.run(["cat", "/dev/null"], check=True)
            yield "base"
    """)
    # Test file overrides shared_fixture with an untagged version. The two
    # FixtureDefs in the closure (base in conftest + override in test file)
    # trigger _collect_fixture_covered_resources to raise during setup.
    pytester.makepyfile("""
        import pytest

        @pytest.fixture
        def shared_fixture():
            yield "override"

        @pytest.mark.cat
        def test_uses_overridden(shared_fixture):
            assert shared_fixture == "override"
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    # The setup-phase ResourceGuardMisconfiguration is reported as an error,
    # and the message must reach the user without being swallowed by a
    # follow-on AttributeError in teardown/makereport.
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*multiple definitions*"])
    assert "AttributeError" not in result.stdout.str()
    assert "_guard_state" not in result.stdout.str()


def test_session_scoped_tagged_fixture_spans_multiple_test_files(
    pytester: pytest.Pytester, clean_guard_env: None
) -> None:
    """A session-scoped tagged fixture is set up once and shared across multiple files."""
    pytester.makeconftest("""
        import subprocess
        import pytest

        from imbue.resource_guards.resource_guards import (
            fixture_uses_resources,
            register_resource_guard,
            start_resource_guards,
            stop_resource_guards,
        )

        register_resource_guard("cat")

        def pytest_configure(config):
            config.addinivalue_line("markers", "cat: test uses cat")

        def pytest_sessionstart(session):
            start_resource_guards(session)

        def pytest_sessionfinish(session, exitstatus):
            stop_resource_guards()

        @pytest.fixture(scope="session")
        @fixture_uses_resources("cat")
        def session_cat_fixture():
            subprocess.run(["cat", "/dev/null"], check=True)
            yield "value"
    """)
    pytester.makepyfile(
        **{
            "test_one": """
                import pytest

                @pytest.mark.cat
                def test_in_file_one(session_cat_fixture):
                    assert session_cat_fixture == "value"
            """,
            "test_two": """
                import pytest

                @pytest.mark.cat
                def test_in_file_two(session_cat_fixture):
                    assert session_cat_fixture == "value"
            """,
        }
    )
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)


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

    # The outer session registered its _ResourceGuardPlugin at pytest_sessionstart. Capture
    # it so we can verify stop_resource_guards() does NOT rip that plugin out when this
    # test's start_resource_guards() was a no-op for plugin registration.
    outer_plugin = request.config.pluginmanager.get_plugin("resource_guards")

    start_resource_guards(request.session)

    assert resource_guards._guard_wrapper_dir is not None
    assert install_called == [1]
    assert request.config.pluginmanager.get_plugin("resource_guards") is not None

    stop_resource_guards()

    assert resource_guards._guard_wrapper_dir is None
    assert cleanup_called == [1]
    # Regression: the outer session's plugin must survive stop_resource_guards(), since this
    # test's start call did not register it. Without ownership tracking in start/stop, the
    # outer plugin would be unregistered here and every subsequent test in the session
    # would lose its runtest_setup/teardown hooks.
    assert request.config.pluginmanager.get_plugin("resource_guards") is outer_plugin


def test_start_and_stop_resource_guards_owner_case_unregisters_plugin(
    isolated_guard_state: None,
    request: pytest.FixtureRequest,
) -> None:
    """Owner case: when start_resource_guards() registers the plugin, stop_resource_guards()
    must unregister it and clear the ownership globals.

    Complements test_start_and_stop_resource_guards_round_trip, which covers the non-owner
    case. Together they enforce the per-caller ownership invariant: start/stop are
    symmetric, and stop only undoes what this particular start registered.
    """
    pluginmanager = request.config.pluginmanager
    # Temporarily pull the outer session's plugin so start_resource_guards() sees an
    # empty slot and takes the owner branch. Restore it in finally so subsequent tests
    # keep their runtest_setup/teardown hooks.
    outer_plugin = pluginmanager.get_plugin("resource_guards")
    assert outer_plugin is not None
    pluginmanager.unregister(outer_plugin)
    try:
        assert pluginmanager.get_plugin("resource_guards") is None

        start_resource_guards(request.session)

        assert resource_guards._owns_guard_plugin is True
        assert resource_guards._guard_plugin is not None
        assert resource_guards._guard_plugin is not outer_plugin
        assert resource_guards._guard_plugin_manager is pluginmanager
        assert pluginmanager.get_plugin("resource_guards") is resource_guards._guard_plugin

        stop_resource_guards()

        assert resource_guards._owns_guard_plugin is False
        assert resource_guards._guard_plugin is None
        assert resource_guards._guard_plugin_manager is None
        assert pluginmanager.get_plugin("resource_guards") is None
    finally:
        if pluginmanager.get_plugin("resource_guards") is None:
            pluginmanager.register(outer_plugin, "resource_guards")


# ---------------------------------------------------------------------------
# SDK guard lifecycle (unit tests)
# ---------------------------------------------------------------------------


def test_register_sdk_guard_adds_entry(isolated_guard_state: None) -> None:
    install_called = []
    register_sdk_guard("test_sdk", lambda: install_called.append(1), lambda: None)

    assert len(resource_guards._registered_sdk_guards) == 1
    assert resource_guards._registered_sdk_guards[0][0] == "test_sdk"
    # Guard name is in _guarded_resources (added by register_sdk_guard).
    assert "test_sdk" in resource_guards._guarded_resources


def test_register_sdk_guard_deduplicates(isolated_guard_state: None) -> None:
    register_sdk_guard("test_sdk", lambda: None, lambda: None)
    register_sdk_guard("test_sdk", lambda: None, lambda: None)

    assert len(resource_guards._registered_sdk_guards) == 1


def test_create_sdk_resource_guards_calls_install(
    isolated_guard_state: None,
) -> None:
    install_called = []
    register_sdk_guard("test_sdk", lambda: install_called.append(1), lambda: None)
    create_sdk_resource_guards()

    assert install_called == [1]


def test_cleanup_sdk_resource_guards_calls_cleanup(
    isolated_guard_state: None,
) -> None:
    cleanup_called = []
    register_sdk_guard("test_sdk", lambda: None, lambda: cleanup_called.append(1))
    cleanup_sdk_resource_guards()

    assert cleanup_called == [1]


def test_get_guarded_resource_names_returns_binary_and_sdk_guards(
    isolated_guard_state: None,
) -> None:
    """get_guarded_resource_names() returns names from both registration paths."""
    register_resource_guard("binary_guard")
    register_sdk_guard("sdk_guard", lambda: None, lambda: None)

    names = get_guarded_resource_names()
    assert "binary_guard" in names
    assert "sdk_guard" in names


def test_sdk_only_guard_does_not_create_binary_wrapper(
    isolated_guard_state: None,
) -> None:
    """SDK-only guard names must not produce PATH wrapper scripts.

    Binary wrappers are only meaningful for names registered via
    register_resource_guard(). Creating a wrapper for an SDK-only guard
    would silently shadow any binary with the same name on PATH.
    """
    register_sdk_guard("sdk_only", lambda: None, lambda: None)
    create_resource_guard_wrappers()

    wrapper_dir = resource_guards._guard_wrapper_dir
    assert wrapper_dir is not None
    assert not (Path(wrapper_dir) / "sdk_only").exists()

    cleanup_resource_guard_wrappers()


def test_register_guarded_resource_markers(
    isolated_guard_state: None,
    pytestconfig: pytest.Config,
) -> None:
    """register_guarded_resource_markers registers marks on the config."""
    register_resource_guard("test_res_a")
    register_resource_guard("test_res_b")

    register_guarded_resource_markers(pytestconfig, skip_names={"test_res_a"})

    marker_names = {m.split(":")[0] for m in pytestconfig.getini("markers")}
    assert "test_res_b" in marker_names
    assert "test_res_a" not in marker_names


def test_register_all_resource_guards_runs_entry_point_callables(
    isolated_guard_state: None,
) -> None:
    """register_all_resource_guards() invokes every callable returned by entry_points()."""

    class _FakeEntryPoint:
        def __init__(self, name: str, callable_: Callable[[], None]) -> None:
            self.name = name
            self._callable = callable_

        def load(self) -> Callable[[], None]:
            return self._callable

    calls: list[str] = []

    def _register_alpha() -> None:
        calls.append("alpha")
        register_resource_guard("alpha")

    def _register_beta() -> None:
        calls.append("beta")
        register_sdk_guard("beta", lambda: None, lambda: None)

    fake_entry_points = [
        _FakeEntryPoint("alpha", _register_alpha),
        _FakeEntryPoint("beta", _register_beta),
    ]

    def _fake_entry_points_fn(*, group: str) -> list[_FakeEntryPoint]:
        assert group == resource_guards.RESOURCE_GUARDS_ENTRY_POINT_GROUP
        return fake_entry_points

    register_all_resource_guards(entry_points=_fake_entry_points_fn)

    assert calls == ["alpha", "beta"]
    names = get_guarded_resource_names()
    assert "alpha" in names
    assert "beta" in names

    # Calling again must be safe -- per-name dedup keeps the registry stable.
    register_all_resource_guards(entry_points=_fake_entry_points_fn)
    names_after = get_guarded_resource_names()
    assert names_after == names


def test_register_guarded_resource_markers_no_skip(
    isolated_guard_state: None,
    pytestconfig: pytest.Config,
) -> None:
    """register_guarded_resource_markers with no skip_names registers all."""
    register_resource_guard("test_all")

    register_guarded_resource_markers(pytestconfig)

    marker_names = {m.split(":")[0] for m in pytestconfig.getini("markers")}
    assert "test_all" in marker_names


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
    # asyncio.get_event_loop() is deprecated and now raises RuntimeError in
    # CI when there's no running loop; use asyncio.run() which manages a
    # fresh loop per call.
    with pytest.raises(ResourceGuardViolation):
        asyncio.run(Client().call(5))

    # Guard allows
    monkeypatch.setenv("_PYTEST_GUARD_TEST_ASYNC", "allow")
    result = asyncio.run(Client().call(5))
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
        asyncio.run(collect_blocked())

    # Guard allows
    monkeypatch.setenv("_PYTEST_GUARD_TEST_AGEN", "allow")

    async def collect_allowed():
        results = []
        async for item in Client().stream():
            results.append(item)
        return results

    results = asyncio.run(collect_allowed())
    assert results == [1, 2]

    # Cleanup restores
    cleanup_sdk_resource_guards()
    assert Client.stream is original_stream


# ---------------------------------------------------------------------------
# _build_guard_env (unit tests)
# ---------------------------------------------------------------------------


def test_build_guard_env_sets_allow_for_marked_resources(
    isolated_guard_state: None,
) -> None:
    register_resource_guard("tmux")
    register_resource_guard("rsync")
    env = _build_guard_env({"tmux"}, "/tmp/track")

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


def _make_state(
    tmp_path: Path,
    marks: set[str],
    *,
    covered_resources: set[str] | None = None,
) -> _PerTestGuardState:
    tracking_dir = str(tmp_path)
    return _PerTestGuardState(
        tracking_dir=tracking_dir,
        marks=marks,
        covered_resources=covered_resources or set(),
        env_patcher=None,
    )


class _FakeFixtureDef:
    """Minimal stand-in for pytest's FixtureDef for unit-testing the hookwrapper."""

    def __init__(self, func: Callable[..., Any], argname: str) -> None:
        self.func = func
        self.argname = argname


class _FakeOutcome:
    """Minimal stand-in for the Result object yielded into a pytest hookwrapper."""

    def __init__(self, excinfo: object | None) -> None:
        self.excinfo = excinfo


class _FakeFixtureRequest:
    """Minimal stand-in for pytest.FixtureRequest used by _pytest_fixture_setup.

    Supports getfixturevalue("tmp_path_factory") (the hook needs pytest's
    session-scoped tmp dir factory) and addfinalizer (the hook registers
    the at-scope-end check as a finalizer). Tests can call run_finalizers()
    to simulate pytest's scope-end teardown.
    """

    def __init__(self, tmp_path_factory: pytest.TempPathFactory | None = None) -> None:
        self._tmp_path_factory = tmp_path_factory
        self.finalizers: list[Callable[[], None]] = []

    def getfixturevalue(self, name: str) -> Any:
        assert name == "tmp_path_factory", f"_FakeFixtureRequest only supports tmp_path_factory, got {name!r}"
        return self._tmp_path_factory

    def addfinalizer(self, finalizer: Callable[[], None]) -> None:
        self.finalizers.append(finalizer)

    def run_finalizers(self) -> None:
        """Run registered finalizers in LIFO order, matching pytest's behavior."""
        for finalizer in reversed(self.finalizers):
            finalizer()


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


def test_check_guard_violations_skips_superfluous_check_when_mark_is_fixture_covered(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """A passing test whose mark is satisfied by a tagged fixture in its closure should pass."""
    register_resource_guard("cat")

    state = _make_state(tmp_path, marks={"cat"}, covered_resources={"cat"})
    report = _FakeReport(passed=True)
    _check_guard_violations(state, report)  # ty: ignore[invalid-argument-type]

    assert report.outcome == "passed"


def test_check_guard_violations_covered_resources_does_not_suppress_blocked(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """Fixture coverage relaxes only the NEVER_INVOKED check, never BLOCKED.

    A blocked invocation by the test body must still be reported even when
    a tagged fixture covers the same resource -- the test body's calls are
    governed by the test's own marks, not the fixture's declaration.
    """
    register_resource_guard("cat")
    (tmp_path / "blocked_cat").touch()

    state = _make_state(tmp_path, marks=set(), covered_resources={"cat"})
    report = _FakeReport(passed=True)
    _check_guard_violations(state, report)  # ty: ignore[invalid-argument-type]

    assert report.outcome == "failed"
    assert "without @pytest.mark.cat" in str(report.longrepr)


def test_check_guard_violations_fails_unmarked_consumer_of_tagged_fixture(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """A passing test consuming a tagged fixture without the matching mark should be failed."""
    register_resource_guard("cat")

    state = _make_state(tmp_path, marks=set(), covered_resources={"cat"})
    report = _FakeReport(passed=True)
    _check_guard_violations(state, report)  # ty: ignore[invalid-argument-type]

    assert report.outcome == "failed"
    assert "missing @pytest.mark.cat" in str(report.longrepr)


def test_check_guard_violations_appends_undeclared_coverage_to_failing_test(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """A failing test that's also missing a mark for a covered fixture gets both messages."""
    register_resource_guard("cat")

    state = _make_state(tmp_path, marks=set(), covered_resources={"cat"})
    report = _FakeReport(passed=False, longrepr="original failure")
    _check_guard_violations(state, report)  # ty: ignore[invalid-argument-type]

    assert report.outcome == "failed"
    assert "original failure" in str(report.longrepr)
    assert "missing @pytest.mark.cat" in str(report.longrepr)


def test_check_guard_violations_does_not_complain_when_mark_matches_coverage(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """When marks fully cover the tagged-fixture resources, no violation should fire."""
    register_resource_guard("cat")

    state = _make_state(tmp_path, marks={"cat"}, covered_resources={"cat"})
    report = _FakeReport(passed=True)
    _check_guard_violations(state, report)  # ty: ignore[invalid-argument-type]

    assert report.outcome == "passed"


def test_check_guard_violations_reports_every_undeclared_mark_at_once(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """When a test misses multiple fixture-covered marks, all of them are reported together."""
    register_resource_guard("cat")
    register_resource_guard("ls")

    state = _make_state(tmp_path, marks=set(), covered_resources={"cat", "ls"})
    report = _FakeReport(passed=True)
    _check_guard_violations(state, report)  # ty: ignore[invalid-argument-type]

    assert report.outcome == "failed"
    longrepr = str(report.longrepr)
    assert "missing @pytest.mark.cat" in longrepr
    assert "missing @pytest.mark.ls" in longrepr


def test_check_guard_violations_reports_blocked_and_undeclared_together(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """A test with both a BLOCKED invocation and an undeclared fixture-covered mark gets both messages.

    The undeclared-fixture-coverage check is a static property of the test
    closure and is independent of the runtime BLOCKED check. Reporting them
    together lets the user fix both in one pass instead of rediscovering the
    second across a rerun.
    """
    register_resource_guard("cat")
    register_resource_guard("ls")
    (tmp_path / "blocked_cat").touch()

    state = _make_state(tmp_path, marks=set(), covered_resources={"ls"})
    report = _FakeReport(passed=True)
    _check_guard_violations(state, report)  # ty: ignore[invalid-argument-type]

    assert report.outcome == "failed"
    longrepr = str(report.longrepr)
    assert "without @pytest.mark.cat" in longrepr
    assert "missing @pytest.mark.ls" in longrepr


# ---------------------------------------------------------------------------
# Fixture-scope helpers (unit tests)
# ---------------------------------------------------------------------------


def test_fixture_uses_resources_records_declaration(isolated_guard_state: None) -> None:
    """The decorator should record the function's declared resources."""

    def some_fixture() -> None:
        pass

    fixture_uses_resources("modal")(some_fixture)
    # Look up via the module attribute rather than the directly-imported name,
    # so that isolated_guard_state's rebind of the dict is observed here.
    assert resource_guards._fixture_resource_marks[some_fixture] == {"modal"}


def test_fixture_uses_resources_supports_multiple_resources_in_one_call(isolated_guard_state: None) -> None:
    """A single call accepts multiple resources and records them as a set."""

    def some_fixture() -> None:
        pass

    fixture_uses_resources("modal", "docker")(some_fixture)
    # Look up via the module attribute rather than the directly-imported name,
    # so that isolated_guard_state's rebind of the dict is observed here.
    assert resource_guards._fixture_resource_marks[some_fixture] == {"modal", "docker"}


def test_fixture_uses_resources_errors_on_double_application(isolated_guard_state: None) -> None:
    """Applying the decorator twice to the same function should raise."""

    def some_fixture() -> None:
        pass

    fixture_uses_resources("modal")(some_fixture)
    with pytest.raises(ResourceGuardMisconfiguration, match="applied more than once"):
        fixture_uses_resources("docker")(some_fixture)


class _FakeFixtureInfo:
    """Minimal stand-in for pytest's FuncFixtureInfo."""

    def __init__(self, name2fixturedefs: dict[str, list[Any]]) -> None:
        self.name2fixturedefs = name2fixturedefs


class _FakeItem:
    """Minimal stand-in for a pytest.Function item with a fixture closure."""

    def __init__(self, name2fixturedefs: dict[str, list[Any]]) -> None:
        self._fixtureinfo = _FakeFixtureInfo(name2fixturedefs)


def test_collect_fixture_covered_resources_unions_across_closure() -> None:
    """Resources declared by any tagged fixture in the closure should be returned."""

    def tagged_fixture_a() -> None:
        pass

    def tagged_fixture_b() -> None:
        pass

    def untagged_fixture() -> None:
        pass

    fixture_uses_resources("modal")(tagged_fixture_a)
    fixture_uses_resources("docker")(tagged_fixture_b)

    item = _FakeItem(
        {
            "tagged_a": [_FakeFixtureDef(tagged_fixture_a, "tagged_a")],
            "tagged_b": [_FakeFixtureDef(tagged_fixture_b, "tagged_b")],
            "plain": [_FakeFixtureDef(untagged_fixture, "plain")],
        }
    )

    covered = _collect_fixture_covered_resources(item)  # ty: ignore[invalid-argument-type]
    assert covered == {"modal", "docker"}


def test_collect_fixture_covered_resources_returns_empty_when_no_tagged_fixtures() -> None:
    """A closure of only untagged fixtures should produce an empty covered set."""

    def plain_fixture() -> None:
        pass

    item = _FakeItem({"plain": [_FakeFixtureDef(plain_fixture, "plain")]})
    assert _collect_fixture_covered_resources(item) == set()  # ty: ignore[invalid-argument-type]


def test_collect_fixture_covered_resources_errors_on_tagged_override() -> None:
    """An override of a tagged fixture is ambiguous and should error.

    Whether the override inherits, replaces, or merges the parent's
    @fixture_uses_resources declaration is undecided -- we have no real
    use case yet. The helper should refuse instead of silently picking.
    """

    def base_fixture() -> None:
        pass

    def override_fixture() -> None:
        pass

    fixture_uses_resources("cat")(base_fixture)

    item = _FakeItem(
        {
            "shared_name": [
                _FakeFixtureDef(base_fixture, "shared_name"),
                _FakeFixtureDef(override_fixture, "shared_name"),
            ],
        }
    )

    with pytest.raises(ResourceGuardMisconfiguration, match="multiple definitions"):
        _collect_fixture_covered_resources(item)  # ty: ignore[invalid-argument-type]


def test_collect_fixture_covered_resources_allows_override_when_none_tagged() -> None:
    """Overrides of fully-untagged fixtures are fine -- the helper has no opinion on them."""

    def base_fixture() -> None:
        pass

    def override_fixture() -> None:
        pass

    item = _FakeItem(
        {
            "shared_name": [
                _FakeFixtureDef(base_fixture, "shared_name"),
                _FakeFixtureDef(override_fixture, "shared_name"),
            ],
        }
    )

    assert _collect_fixture_covered_resources(item) == set()  # ty: ignore[invalid-argument-type]


def test_detect_guard_violations_returns_blocked_when_present(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """A blocked_<resource> file should report a BLOCKED violation."""
    register_resource_guard("cat")
    (tmp_path / "blocked_cat").touch()

    violation = _detect_guard_violations(set(), str(tmp_path), check_never_invoked=True)
    assert violation == _GuardViolation(resource="cat", kind=_GuardViolationKind.BLOCKED)


def test_detect_guard_violations_returns_never_invoked_for_unused_mark(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """A guarded resource in marks with no tracking file should report NEVER_INVOKED."""
    register_resource_guard("cat")

    violation = _detect_guard_violations({"cat"}, str(tmp_path), check_never_invoked=True)
    assert violation == _GuardViolation(resource="cat", kind=_GuardViolationKind.NEVER_INVOKED)


def test_detect_guard_violations_skips_never_invoked_when_flag_false(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """When check_never_invoked is False, an unused mark should not produce a violation."""
    register_resource_guard("cat")

    assert _detect_guard_violations({"cat"}, str(tmp_path), check_never_invoked=False) is None


def test_detect_guard_violations_ignores_non_guarded_marks(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """Marks not registered as guarded resources should never trigger never-invoked."""
    register_resource_guard("cat")

    assert _detect_guard_violations({"xdist_group"}, str(tmp_path), check_never_invoked=True) is None


def test_detect_guard_violations_returns_none_when_clean(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """A tracking_dir with the expected tracking file and no blocked files should be clean."""
    register_resource_guard("cat")
    (tmp_path / "cat").touch()

    assert _detect_guard_violations({"cat"}, str(tmp_path), check_never_invoked=True) is None


def test_make_guarded_fixture_wrapper_generator_applies_env_on_setup_and_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setup and teardown should see fixture_env; consumer phase should not."""
    fixture_env = {"_TEST_FIXTURE_FLAG": "active"}
    seen_at_setup: list[str | None] = []
    seen_at_teardown: list[str | None] = []

    def original() -> Generator[str, None, None]:
        seen_at_setup.append(os.environ.get("_TEST_FIXTURE_FLAG"))
        yield "value"
        seen_at_teardown.append(os.environ.get("_TEST_FIXTURE_FLAG"))

    monkeypatch.delenv("_TEST_FIXTURE_FLAG", raising=False)
    wrapped = _make_guarded_fixture_wrapper(original, fixture_env)
    gen = wrapped()
    value = next(gen)
    consumer_view = os.environ.get("_TEST_FIXTURE_FLAG")
    for _ in gen:
        pass

    assert value == "value"
    assert seen_at_setup == ["active"]
    assert consumer_view is None
    assert seen_at_teardown == ["active"]


def test_make_guarded_fixture_wrapper_plain_fixture_applies_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-generator fixture should run with fixture_env active and restore afterward."""
    fixture_env = {"_TEST_FIXTURE_FLAG": "active"}
    seen: list[str | None] = []

    def original() -> str:
        seen.append(os.environ.get("_TEST_FIXTURE_FLAG"))
        return "value"

    monkeypatch.delenv("_TEST_FIXTURE_FLAG", raising=False)
    wrapped = _make_guarded_fixture_wrapper(original, fixture_env)
    result = wrapped()

    assert result == "value"
    assert seen == ["active"]
    assert os.environ.get("_TEST_FIXTURE_FLAG") is None


# --- _check_fixture_blocked_after_setup ---


def test_check_fixture_blocked_after_setup_passes_when_clean(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """No tracking files at all should be silent (no BLOCKED to report; NEVER_INVOKED deferred)."""
    register_resource_guard("cat")

    _check_fixture_blocked_after_setup("my_fixture", {"cat"}, str(tmp_path))


def test_check_fixture_blocked_after_setup_passes_when_resource_invoked(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """A declared resource that was invoked during setup is fine."""
    register_resource_guard("cat")
    (tmp_path / "cat").touch()

    _check_fixture_blocked_after_setup("my_fixture", {"cat"}, str(tmp_path))


def test_check_fixture_blocked_after_setup_raises_on_blocked(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """A fixture that invoked an undeclared resource during setup should raise immediately."""
    register_resource_guard("cat")
    (tmp_path / "blocked_cat").touch()

    with pytest.raises(ResourceGuardViolation, match="did not declare it"):
        _check_fixture_blocked_after_setup("my_fixture", set(), str(tmp_path))


def test_check_fixture_blocked_after_setup_does_not_raise_on_never_invoked(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """NEVER_INVOKED is deferred -- the setup-time check should ignore it."""
    register_resource_guard("cat")

    # No tracking files at all (would trigger NEVER_INVOKED in the deferred check).
    _check_fixture_blocked_after_setup("my_fixture", {"cat"}, str(tmp_path))


def test_check_fixture_blocked_after_setup_chains_setup_exception(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """When setup raised and a BLOCKED violation fires, the original exception is chained as __cause__."""
    register_resource_guard("cat")
    (tmp_path / "blocked_cat").touch()

    original = RuntimeError("setup failed for unrelated reason")
    with pytest.raises(ResourceGuardViolation) as exc_info:
        _check_fixture_blocked_after_setup(
            "my_fixture",
            set(),
            str(tmp_path),
            setup_exception=original,
        )

    assert exc_info.value.__cause__ is original


# --- _check_fixture_at_scope_end ---


def test_check_fixture_at_scope_end_passes_when_clean(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """A fixture that invoked its declared resource (in setup or teardown) passes the deferred check."""
    register_resource_guard("cat")
    (tmp_path / "cat").touch()

    _check_fixture_at_scope_end("my_fixture", {"cat"}, str(tmp_path), setup_failed=False)


def test_check_fixture_at_scope_end_raises_on_blocked_in_teardown(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """A blocked tracking file that only appeared during teardown should still be caught."""
    register_resource_guard("cat")
    (tmp_path / "blocked_cat").touch()

    with pytest.raises(ResourceGuardViolation, match="did not declare it"):
        _check_fixture_at_scope_end("my_fixture", set(), str(tmp_path), setup_failed=False)


def test_check_fixture_at_scope_end_raises_on_never_invoked(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """A declared resource never invoked in setup or teardown should raise."""
    register_resource_guard("cat")

    with pytest.raises(ResourceGuardViolation, match="did not invoke cat during setup or teardown"):
        _check_fixture_at_scope_end("my_fixture", {"cat"}, str(tmp_path), setup_failed=False)


def test_check_fixture_at_scope_end_skips_when_setup_failed(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """When setup raised, the deferred check is suppressed (no teardown ran; never_invoked is misleading)."""
    register_resource_guard("cat")
    # No tracking files; ordinarily would raise NEVER_INVOKED, but setup_failed suppresses it.
    _check_fixture_at_scope_end("my_fixture", {"cat"}, str(tmp_path), setup_failed=True)


def test_check_fixture_at_scope_end_skips_blocked_when_setup_failed(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """When setup raised, even a stray BLOCKED tracking file is ignored at scope-end (setup check fired)."""
    register_resource_guard("cat")
    (tmp_path / "blocked_cat").touch()

    _check_fixture_at_scope_end("my_fixture", set(), str(tmp_path), setup_failed=True)


def test_check_fixture_at_scope_end_multi_resource_one_invoked_one_not(
    isolated_guard_state: None,
    tmp_path: Path,
) -> None:
    """When a fixture declares two resources but only invokes one, the other still fires NEVER_INVOKED."""
    register_resource_guard("cat")
    register_resource_guard("ls")
    (tmp_path / "cat").touch()

    with pytest.raises(ResourceGuardViolation, match="did not invoke ls during setup or teardown"):
        _check_fixture_at_scope_end("my_fixture", {"cat", "ls"}, str(tmp_path), setup_failed=False)


def test_pytest_fixture_setup_skips_undeclared_fixture() -> None:
    """An ordinary fixture without @fixture_uses_resources should pass through untouched."""

    def some_fixture() -> str:
        return "value"

    fixturedef = _FakeFixtureDef(func=some_fixture, argname="some_fixture")
    request = _FakeFixtureRequest()
    hook = _pytest_fixture_setup(fixturedef, request=request)  # ty: ignore[invalid-argument-type]

    # Hookwrapper yields once.
    next(hook)
    with pytest.raises(StopIteration):
        hook.send(_FakeOutcome(excinfo=None))  # ty: ignore[invalid-argument-type]

    assert fixturedef.func is some_fixture


def test_pytest_fixture_setup_wraps_declared_fixture_and_restores_on_exit(
    isolated_guard_state: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A declared fixture should be wrapped during setup and restored after the hookwrapper exits."""
    register_resource_guard("cat")

    @fixture_uses_resources("cat")
    def some_fixture() -> Generator[str, None, None]:
        # Simulate the fixture's setup calling cat by manually touching the tracking file.
        Path(os.environ["_PYTEST_GUARD_TRACKING_DIR"]).joinpath("cat").touch()
        yield "value"

    fixturedef = _FakeFixtureDef(func=some_fixture, argname="some_fixture")
    request = _FakeFixtureRequest(tmp_path_factory=tmp_path_factory)
    hook = _pytest_fixture_setup(fixturedef, request=request)  # ty: ignore[invalid-argument-type]

    next(hook)
    # fixturedef.func has been replaced with the wrapper. Drive it to exercise setup.
    gen = fixturedef.func()
    value = next(gen)
    assert value == "value"
    for _ in gen:
        pass

    # Closing the hookwrapper restores fixturedef.func and runs the inline BLOCKED check.
    with pytest.raises(StopIteration):
        hook.send(_FakeOutcome(excinfo=None))  # ty: ignore[invalid-argument-type]
    assert fixturedef.func is some_fixture
    # The deferred at-scope-end check is registered as a finalizer.
    assert len(request.finalizers) == 1
    # Running it here (simulating pytest's scope-end teardown) should be clean
    # since cat was invoked.
    request.run_finalizers()


def test_pytest_fixture_setup_defers_never_invoked_check_until_finalizer(
    isolated_guard_state: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """NEVER_INVOKED does not fire at hookwrapper close -- only when finalizers run."""
    register_resource_guard("cat")

    @fixture_uses_resources("cat")
    def empty_fixture() -> Generator[str, None, None]:
        # Setup invokes nothing. NEVER_INVOKED would fire if the inline check
        # ran it; it must wait for the at-scope-end finalizer.
        yield "value"

    fixturedef = _FakeFixtureDef(func=empty_fixture, argname="empty_fixture")
    request = _FakeFixtureRequest(tmp_path_factory=tmp_path_factory)
    hook = _pytest_fixture_setup(fixturedef, request=request)  # ty: ignore[invalid-argument-type]

    next(hook)
    gen = fixturedef.func()
    next(gen)
    for _ in gen:
        pass

    # Hookwrapper close: the inline BLOCKED check is clean; no error.
    with pytest.raises(StopIteration):
        hook.send(_FakeOutcome(excinfo=None))  # ty: ignore[invalid-argument-type]

    # Running the deferred finalizer fires NEVER_INVOKED.
    with pytest.raises(ResourceGuardViolation, match="did not invoke cat during setup or teardown"):
        request.run_finalizers()


def test_pytest_fixture_setup_at_scope_end_skipped_when_setup_failed(
    isolated_guard_state: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """If setup raised, the at-scope-end finalizer must not fire NEVER_INVOKED."""
    register_resource_guard("cat")

    @fixture_uses_resources("cat")
    def empty_fixture() -> Generator[str, None, None]:
        yield "value"

    fixturedef = _FakeFixtureDef(func=empty_fixture, argname="empty_fixture")
    request = _FakeFixtureRequest(tmp_path_factory=tmp_path_factory)
    hook = _pytest_fixture_setup(fixturedef, request=request)  # ty: ignore[invalid-argument-type]

    next(hook)
    gen = fixturedef.func()
    next(gen)
    for _ in gen:
        pass

    # Simulate inner setup raising: outcome.excinfo is non-None.
    original = RuntimeError("inner setup boom")
    with pytest.raises(StopIteration):
        hook.send(_FakeOutcome(excinfo=(type(original), original, None)))  # ty: ignore[invalid-argument-type]

    # The deferred finalizer runs but skips both checks (setup_failed=True).
    request.run_finalizers()


def test_pytest_fixture_setup_raises_on_undeclared_fixture_call(
    isolated_guard_state: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A declared fixture that invokes an undeclared resource should raise on hookwrapper close."""
    register_resource_guard("cat")

    @fixture_uses_resources("ls")
    def some_fixture() -> Generator[str, None, None]:
        Path(os.environ["_PYTEST_GUARD_TRACKING_DIR"]).joinpath("blocked_cat").touch()
        yield "value"

    fixturedef = _FakeFixtureDef(func=some_fixture, argname="some_fixture")
    request = _FakeFixtureRequest(tmp_path_factory=tmp_path_factory)
    hook = _pytest_fixture_setup(fixturedef, request=request)  # ty: ignore[invalid-argument-type]

    next(hook)
    gen = fixturedef.func()
    next(gen)
    for _ in gen:
        pass

    with pytest.raises(ResourceGuardViolation, match="did not declare it"):
        hook.send(_FakeOutcome(excinfo=None))  # ty: ignore[invalid-argument-type]


def test_pytest_fixture_setup_captures_setup_exception_for_chaining(
    isolated_guard_state: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """When the inner setup raised, the hook captures the exception to chain it onto a guard violation."""
    register_resource_guard("cat")

    @fixture_uses_resources("cat")
    def some_fixture() -> Generator[str, None, None]:
        # Simulate the blocked-during-setup case: tracking file is present,
        # AND the inner fixture setup raised (excinfo non-None).
        Path(os.environ["_PYTEST_GUARD_TRACKING_DIR"]).joinpath("blocked_cat").touch()
        yield "value"

    fixturedef = _FakeFixtureDef(func=some_fixture, argname="some_fixture")
    request = _FakeFixtureRequest(tmp_path_factory=tmp_path_factory)
    hook = _pytest_fixture_setup(fixturedef, request=request)  # ty: ignore[invalid-argument-type]

    next(hook)
    gen = fixturedef.func()
    next(gen)
    for _ in gen:
        pass

    original = RuntimeError("inner setup boom")
    outcome = _FakeOutcome(excinfo=(type(original), original, None))
    with pytest.raises(ResourceGuardViolation) as exc_info:
        hook.send(outcome)  # ty: ignore[invalid-argument-type]
    # The captured setup_exception should be chained as __cause__.
    assert exc_info.value.__cause__ is original


def test_pytest_runtest_setup_defers_closure_violation_until_after_yield(
    isolated_guard_state: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ResourceGuardMisconfiguration from closure inspection is held until after inner setup yields."""
    register_resource_guard("cat")

    # _pytest_runtest_setup asserts _guard_wrapper_dir is not None; pretend
    # the wrapper directory was created.
    monkeypatch.setattr(resource_guards, "_guard_wrapper_dir", "/tmp/fake-wrapper-dir")

    # Build an item whose fixture closure forces _collect_fixture_covered_resources
    # to raise: an override (two FixtureDefs under the same name) where one is tagged.
    def tagged_fixture() -> None:
        pass

    def override_fixture() -> None:
        pass

    fixture_uses_resources("cat")(tagged_fixture)

    fixture_info = _FakeFixtureInfo(
        {
            "shared": [
                _FakeFixtureDef(tagged_fixture, "shared"),
                _FakeFixtureDef(override_fixture, "shared"),
            ],
        }
    )

    class _FakeMarker:
        name = "cat"

    class _FakeItem:
        def __init__(self) -> None:
            self._fixtureinfo = fixture_info
            self._guard_state: _PerTestGuardState | None = None

        def iter_markers(self) -> list[_FakeMarker]:
            return [_FakeMarker()]

    item = _FakeItem()
    hook = _pytest_runtest_setup(item)  # ty: ignore[invalid-argument-type]

    # The hook should yield without raising even though the closure walk raised.
    next(hook)

    # Guard state should be initialized so teardown/makereport don't crash.
    assert item._guard_state is not None
    assert item._guard_state.tracking_dir.startswith("/")

    # After the inner setup completes (yield resumes), the held error is raised.
    with pytest.raises(ResourceGuardMisconfiguration, match="multiple definitions"):
        with pytest.raises(StopIteration):
            hook.send(None)

    # Clean up the env patcher we started.
    item._guard_state.env_patcher.stop()


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
from imbue.resource_guards.resource_guards import (
    register_sdk_guard,
    start_resource_guards,
    stop_resource_guards,
)

def pytest_configure(config):
    config.addinivalue_line("markers", "test_sdk: test uses test_sdk")

register_sdk_guard("test_sdk", lambda: None, lambda: None)

def pytest_sessionstart(session):
    start_resource_guards(session)

def pytest_sessionfinish(session, exitstatus):
    stop_resource_guards()
"""


def test_sdk_marked_test_that_triggers_guard_passes(pytester: pytest.Pytester, clean_guard_env: None) -> None:
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


def test_sdk_unmarked_test_that_triggers_guard_fails(pytester: pytest.Pytester, clean_guard_env: None) -> None:
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
    clean_guard_env: None,
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


@pytest.mark.flaky
def test_sdk_marked_test_that_never_triggers_guard_fails(
    pytester: pytest.Pytester,
    clean_guard_env: None,
) -> None:
    """A test with the SDK mark that never triggers the guard fails (superfluous mark).

    Marked flaky because the inner pytester subprocess sporadically exceeds the
    default 10s pytest-timeout under CI load.
    """
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
    clean_guard_env: None,
) -> None:
    """A test with no SDK mark and no guard trigger should pass."""
    pytester.makeconftest(_PYTESTER_SDK_CONFTEST)
    pytester.makepyfile("""
        def test_no_sdk():
            assert 1 + 1 == 2
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


# ---------------------------------------------------------------------------
# Mark auto-registration via register_guarded_resource_markers (pytester)
# ---------------------------------------------------------------------------

# Conftest that mirrors the README example: standalone pytest_configure
# using register_guarded_resource_markers() (no conftest_hooks).
_PYTESTER_STANDALONE_CONFTEST = """\
from imbue.resource_guards.resource_guards import (
    register_guarded_resource_markers,
    register_resource_guard,
    start_resource_guards,
    stop_resource_guards,
)

register_resource_guard("cat")

def pytest_configure(config):
    register_guarded_resource_markers(config)

def pytest_sessionstart(session):
    start_resource_guards(session)

def pytest_sessionfinish(session, exitstatus):
    stop_resource_guards()
"""


def test_standalone_pytest_configure_registers_marks(
    pytester: pytest.Pytester,
    clean_guard_env: None,
) -> None:
    """The README pattern (pytest_configure + register_guarded_resource_markers) works.

    Verifies that external users who don't use conftest_hooks can register
    marks via their own pytest_configure and register_guarded_resource_markers().
    """
    pytester.makeconftest(_PYTESTER_STANDALONE_CONFTEST)
    pytester.makepyfile("""
        import subprocess
        import pytest

        @pytest.mark.cat
        def test_cat_dev_null():
            subprocess.run(["cat", "/dev/null"], check=True)
    """)
    result = pytester.runpytest_subprocess("-n0", "--no-header", "-p", "no:cacheprovider", "--strict-markers")
    result.assert_outcomes(passed=1)
