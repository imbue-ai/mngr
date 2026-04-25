"""Drift tests for the constants inlined in cron_runner.py and verification.py.

cron_runner.py is deployed standalone into Modal, where the Python interpreter
does NOT see the imbue namespace. It cannot import from
`imbue.mngr.primitives` or `imbue.mngr_schedule.data_types` at module scope, so
values that mirror those enums (RUNNING_STATES, VALID_VERIFY_MODES) and
sentinel values shared with verification.py (AGENT_MISSING_STATE,
RESULT_SENTINEL) are duplicated as bare literals.

This module reads cron_runner.py and verification.py as source via the AST
module and compares the literals against the authoritative enums, so a
silent drift between the two copies (or between either copy and the enum)
fails CI rather than surfacing as a runtime bug only the release test would
catch. Importing cron_runner.py directly is not viable: its module-level
code reads required deploy-time env vars under modal.is_local() and would
raise at import time on a developer machine.
"""

import ast
from pathlib import Path

from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr_schedule.data_types import VerifyMode

_MODAL_DIR: Path = Path(__file__).parent
_CRON_RUNNER_PATH: Path = _MODAL_DIR / "cron_runner.py"
_VERIFICATION_PATH: Path = _MODAL_DIR / "verification.py"


def _extract_str_literal(source: str, name: str) -> str:
    """Return the string literal assigned to `name` at module scope."""
    tree = ast.parse(source)
    for node in tree.body:
        target_names: list[str] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            target_names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_names = [node.target.id]
            value = node.value
        if name in target_names and isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    raise AssertionError(f"could not find string literal {name!r} at module scope")


def _extract_frozenset_str_literal(source: str, name: str) -> frozenset[str]:
    """Return the frozenset[str] literal assigned to `name` at module scope.

    Recognises both `frozenset({"a", "b"})` and a bare set literal `{"a", "b"}`.
    """
    tree = ast.parse(source)
    for node in tree.body:
        target_names: list[str] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            target_names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_names = [node.target.id]
            value = node.value
        if name not in target_names or value is None:
            continue
        set_node = value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "frozenset":
            if not value.args:
                return frozenset()
            set_node = value.args[0]
        if isinstance(set_node, ast.Set):
            elements: list[str] = []
            for elt in set_node.elts:
                if not isinstance(elt, ast.Constant) or not isinstance(elt.value, str):
                    raise AssertionError(f"non-string element in {name!r} literal: {ast.dump(elt)}")
                elements.append(elt.value)
            return frozenset(elements)
    raise AssertionError(f"could not find frozenset[str] literal {name!r} at module scope")


def test_cron_runner_running_states_match_lifecycle_enum() -> None:
    cron_running_states = _extract_frozenset_str_literal(_CRON_RUNNER_PATH.read_text(), "RUNNING_STATES")
    expected = {
        AgentLifecycleState.RUNNING.value,
        AgentLifecycleState.WAITING.value,
        AgentLifecycleState.REPLACED.value,
        AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE.value,
    }
    assert cron_running_states == frozenset(expected)


def test_agent_lifecycle_state_enum_pinned() -> None:
    """Pin the full enum so any addition or removal forces a manual reconcile.

    Adding a new RUNNING_* variant upstream without updating cron_runner's
    inlined RUNNING_STATES would silently treat the new state as terminal in
    full-verify polling. Pin the closed set here to surface the change.
    """
    assert {state.value for state in AgentLifecycleState} == {
        "RUNNING",
        "WAITING",
        "REPLACED",
        "RUNNING_UNKNOWN_AGENT_TYPE",
        "STOPPED",
        "DONE",
    }


def test_cron_runner_valid_verify_modes_match_verify_mode_enum() -> None:
    cron_valid_modes = _extract_frozenset_str_literal(_CRON_RUNNER_PATH.read_text(), "VALID_VERIFY_MODES")
    assert cron_valid_modes == frozenset(mode.value.lower() for mode in VerifyMode)


def test_agent_missing_state_matches_between_files() -> None:
    cron_missing = _extract_str_literal(_CRON_RUNNER_PATH.read_text(), "AGENT_MISSING_STATE")
    verify_missing = _extract_str_literal(_VERIFICATION_PATH.read_text(), "_AGENT_MISSING_STATE")
    assert cron_missing == verify_missing


def test_agent_missing_state_disjoint_from_lifecycle_enum() -> None:
    """The sentinel must not collide with any real lifecycle state."""
    cron_missing = _extract_str_literal(_CRON_RUNNER_PATH.read_text(), "AGENT_MISSING_STATE")
    assert cron_missing not in {state.value for state in AgentLifecycleState}


def test_result_sentinel_matches_between_files() -> None:
    cron_sentinel = _extract_str_literal(_CRON_RUNNER_PATH.read_text(), "RESULT_SENTINEL")
    verify_sentinel = _extract_str_literal(_VERIFICATION_PATH.read_text(), "_RESULT_SENTINEL")
    assert cron_sentinel == verify_sentinel
