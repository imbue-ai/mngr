"""Pin the per-class test budget that keeps real-tmux tests off the global timeout.

These assert on the budget a test actually resolves to inside a live pytest session, rather
than on the table that supplies it, so they fail if the budget stops reaching the tests.
"""

import subprocess

import pytest

# The slowest tmux-marked test on an uncontended CI runner already takes about 7 seconds, so
# anything below this leaves no room for the parallel contention CI actually runs under.
MINIMUM_TMUX_TEST_BUDGET_SECONDS = 30


@pytest.mark.tmux
def test_a_tmux_test_resolves_to_a_budget_sized_for_real_tmux_work(request: pytest.FixtureRequest) -> None:
    """A tmux test that declares no timeout of its own must not be left on the global default."""
    subprocess.run(["tmux", "-V"], capture_output=True, check=True)

    marker = request.node.get_closest_marker("timeout")
    resolved_budget = marker.args[0] if marker is not None else request.config.getoption("timeout", default=None)

    assert resolved_budget is not None and resolved_budget >= MINIMUM_TMUX_TEST_BUDGET_SECONDS, (
        f"a tmux test with no timeout of its own resolved to a {resolved_budget}s budget, under the "
        f"{MINIMUM_TMUX_TEST_BUDGET_SECONDS}s a real tmux test needs. Every tmux test is now one "
        "flaky CI run away from red; fix the class budget rather than decorating tests one at a time."
    )


def test_an_unmarked_test_is_left_on_the_global_timeout(request: pytest.FixtureRequest) -> None:
    """The class budget applies to its class only, and must not widen everything else with it."""
    assert request.node.get_closest_marker("timeout") is None, (
        "a test in no real-resource class picked up a timeout marker, so the per-class budget is "
        "being applied too broadly and is hiding genuine hangs in ordinary tests."
    )
