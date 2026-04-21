"""Drift detection for cron_runner constants that duplicate imbue types.

cron_runner.py is forbidden from importing `imbue.*` at module level (see
the file-level comment in cron_runner.py), so it inlines the set of
AgentLifecycleState running states as bare string literals. That mirror
can silently drift from the real enum. These tests statically parse the
cron_runner source and assert the inlined set matches the enum, and that
the enum has not grown or shrunk without a human reconciling it.
"""

import ast
from pathlib import Path

from imbue.mngr.primitives import AgentLifecycleState

_CRON_RUNNER_PATH = Path(__file__).parent / "cron_runner.py"

# States that represent an actively-running agent. This mirrors the set
# inlined in cron_runner._RUNNING_STATES; the tests below verify the two
# stay in sync.
_EXPECTED_RUNNING_STATES: frozenset[AgentLifecycleState] = frozenset(
    {
        AgentLifecycleState.RUNNING,
        AgentLifecycleState.WAITING,
        AgentLifecycleState.REPLACED,
        AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE,
    }
)

# Full enumeration of lifecycle states known at the time cron_runner was
# last reviewed. If this set changes, a human must decide whether the new
# state should be considered "running" in the verify loop and update both
# this test and cron_runner._RUNNING_STATES accordingly.
_EXPECTED_ALL_STATES: frozenset[AgentLifecycleState] = frozenset(
    {
        AgentLifecycleState.STOPPED,
        AgentLifecycleState.RUNNING,
        AgentLifecycleState.WAITING,
        AgentLifecycleState.REPLACED,
        AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE,
        AgentLifecycleState.DONE,
    }
)


def _extract_running_states_literals() -> frozenset[str]:
    """Parse cron_runner.py and return the string literals assigned to _RUNNING_STATES."""
    tree = ast.parse(_CRON_RUNNER_PATH.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign):
            continue
        target = node.target
        if not isinstance(target, ast.Name) or target.id != "_RUNNING_STATES":
            continue
        value = node.value
        assert isinstance(value, ast.Call), "_RUNNING_STATES must be assigned from frozenset(...)"
        assert len(value.args) == 1, "frozenset(...) must take one argument"
        arg = value.args[0]
        assert isinstance(arg, ast.Set), "frozenset argument must be a set literal"
        literals: set[str] = set()
        for element in arg.elts:
            assert isinstance(element, ast.Constant) and isinstance(element.value, str), (
                "_RUNNING_STATES must contain only string literals"
            )
            literals.add(element.value)
        return frozenset(literals)
    raise AssertionError("_RUNNING_STATES assignment not found in cron_runner.py")


def test_cron_runner_running_states_match_enum() -> None:
    """cron_runner._RUNNING_STATES (inlined strings) must match the enum mirror."""
    actual = _extract_running_states_literals()
    expected = frozenset(state.value for state in _EXPECTED_RUNNING_STATES)
    assert actual == expected, (
        f"cron_runner._RUNNING_STATES has drifted from AgentLifecycleState: "
        f"cron_runner has {sorted(actual)}, mirror expects {sorted(expected)}. "
        f"Update cron_runner._RUNNING_STATES and this test together."
    )


def test_agent_lifecycle_state_enum_is_unchanged() -> None:
    """Force a human decision whenever the enum grows or shrinks.

    cron_runner inlines a subset of AgentLifecycleState values as strings.
    If the enum changes, the inlined mirror must be revisited -- and so
    must this test's `_EXPECTED_ALL_STATES` after reconciling.
    """
    actual = frozenset(AgentLifecycleState)
    assert actual == _EXPECTED_ALL_STATES, (
        f"AgentLifecycleState has changed: now {sorted(s.value for s in actual)}, "
        f"previously {sorted(s.value for s in _EXPECTED_ALL_STATES)}. "
        f"Update cron_runner._RUNNING_STATES and both sets in cron_runner_test.py "
        f"to reflect whether the new/removed state counts as running."
    )
