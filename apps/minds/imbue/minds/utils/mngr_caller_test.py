import multiprocessing.forkserver
import multiprocessing.resource_tracker
import sys
import urllib.request
from collections.abc import Iterator

import pytest

from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.mngr_caller import _coerce_exit_code
from imbue.minds.utils.mngr_caller import _neutralize_macos_proxy_lookup
from imbue.minds.utils.mngr_caller import _resolve_macos_proxy_state


@pytest.fixture(autouse=True)
def _stop_forkserver_after_test() -> Iterator[None]:
    """Tear down the multiprocessing forkserver + resource_tracker after each test.

    A real :meth:`MngrCaller.call` starts a process-lifetime forkserver (and
    multiprocessing starts a resource_tracker alongside it). In production these
    are reaped at interpreter exit / on parent death, but the per-session leak
    checker runs earlier and would flag them, so tests that start a real
    forkserver must stop it explicitly. Both ``_stop`` calls are safe no-ops
    when nothing was started.
    """
    yield
    # ``_stop`` is the real reset mechanism (multiprocessing itself calls it at
    # exit); it resets internal state so a later call restarts cleanly. It is
    # absent from typeshed, hence the ignores.
    multiprocessing.forkserver._forkserver._stop()  # ty: ignore[unresolved-attribute]
    multiprocessing.resource_tracker._resource_tracker._stop()  # ty: ignore[unresolved-attribute]


def test_coerce_exit_code_none_is_success() -> None:
    assert _coerce_exit_code(None) == 0


def test_coerce_exit_code_passes_through_ints() -> None:
    assert _coerce_exit_code(0) == 0
    assert _coerce_exit_code(2) == 2


def test_coerce_exit_code_string_message_is_failure() -> None:
    # click/SystemExit with a string code conventionally means an error.
    assert _coerce_exit_code("boom") == 1


def test_call_result_defaults() -> None:
    result = MngrCallResult(returncode=0)
    assert result.stdout == ""
    assert result.stderr == ""
    assert result.is_timed_out is False


def test_resolve_macos_proxy_state_matches_platform() -> None:
    # macOS must resolve a (proxies, settings) pair from _scproxy in the parent;
    # everywhere else the proxy lookup is already fork-safe, so there is nothing
    # to resolve and the child neutralization is skipped.
    state = _resolve_macos_proxy_state()
    if sys.platform == "darwin":
        assert state is not None
        proxies, settings = state
        assert isinstance(proxies, dict)
        assert isinstance(settings, dict)
    else:
        assert state is None


def test_neutralize_macos_proxy_lookup_is_noop_for_none() -> None:
    # The off-macOS / nothing-to-install path must not raise.
    _neutralize_macos_proxy_lookup(None)


@pytest.mark.skipif(sys.platform != "darwin", reason="exercises the macOS _scproxy neutralization")
def test_neutralize_macos_proxy_lookup_replaces_scproxy_entry_points() -> None:
    # After neutralization, the macOS proxy lookups return the injected state and
    # never call into SystemConfiguration (the call that segfaults a fork child).
    sentinel_proxies = {"http": "http://proxy.example:8080"}
    sentinel_settings: dict[str, object] = {"exclude_simple": True}
    saved_get_proxies = urllib.request._get_proxies  # ty: ignore[unresolved-attribute]
    saved_get_proxy_settings = urllib.request._get_proxy_settings  # ty: ignore[unresolved-attribute]
    try:
        _neutralize_macos_proxy_lookup((sentinel_proxies, sentinel_settings))
        assert urllib.request.getproxies_macosx_sysconf() == sentinel_proxies  # ty: ignore[unresolved-attribute]
        assert urllib.request._get_proxy_settings() == sentinel_settings  # ty: ignore[unresolved-attribute]
    finally:
        urllib.request._get_proxies = saved_get_proxies  # ty: ignore[unresolved-attribute]
        urllib.request._get_proxy_settings = saved_get_proxy_settings  # ty: ignore[unresolved-attribute]


# These two tests start a real multiprocessing forkserver and preload
# ``imbue.mngr.main`` before forking a child to run the CLI. Under CI load that
# cold start routinely exceeds the 10s global pytest-timeout (the call's own
# timeout is 120s), so give them a generous per-test timeout and mark them flaky
# so offload retries a contended cold start rather than failing the run.
@pytest.mark.flaky
@pytest.mark.timeout(60)
def test_call_runs_mngr_version_in_forkserver_child() -> None:
    """End-to-end: a real ``mngr --version`` runs in a forkserver child.

    This exercises the whole mechanism: starting the forkserver, preloading
    ``imbue.mngr.main``, forking a child, running the CLI, and capturing
    stdout/exit-code. ``--version`` is used because it does no provider
    discovery, so the call is fast and deterministic.

    Marked flaky: forkserver cold-start occasionally exceeds the 10s pytest
    timeout under CI load.
    """
    result = MngrCaller().call(["--version"], timeout=120.0)
    assert result.returncode == 0
    assert result.is_timed_out is False
    assert "mngr" in result.stdout


@pytest.mark.flaky
@pytest.mark.timeout(60)
def test_call_reports_nonzero_exit_for_unknown_command() -> None:
    # Marked flaky: forkserver cold-start occasionally exceeds the 10s pytest
    # timeout under CI load.
    result = MngrCaller().call(["definitely-not-a-real-subcommand"], timeout=120.0)
    assert result.returncode != 0
