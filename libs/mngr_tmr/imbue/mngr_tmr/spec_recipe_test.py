"""Unit tests for spec-anchored TMR discovery.

Filter *semantics* (area segment matching, tag-vs-coordinate) are layer 1's,
tested in ``imbue.mngr_specs``; here we only cover their threading and
AND-composition, plus the grouping/ordering/naming that is this recipe's own.
"""

from pathlib import Path

import pytest

from imbue.mngr_specs.data_types import SpecUnitKind
from imbue.mngr_specs.testing import write_spec_corpus
from imbue.mngr_tmr.spec_recipe import NoSpecUnitsError
from imbue.mngr_tmr.spec_recipe import SpecCorpusInvalidError
from imbue.mngr_tmr.spec_recipe import discover_spec_tasks

_SIGNIN_FEATURE = """Feature: Sign in
  @fresh-code
  Scenario: Opening a fresh login URL signs the user in
    When the user opens the login URL
    Then the user is signed in

  @used-code
  Scenario: A spent code is refused
    When anyone presents a spent code
    Then authentication is refused
"""

_SESSION_FEATURE = """Feature: Session lifetime
  @survives-restart
  Scenario: Sessions survive a restart
    When the client restarts
    Then the user is still signed in
"""

_AUTH_INVARIANTS_FEATURE = """Feature: Authentication invariants
  @single-use-codes
  Rule: A one-time code grants at most one session, ever
"""

_TUNNELS_FEATURE = """Feature: Tunnels
  @no-tls
  Scenario: Tunnels work without TLS
    When a tunnel is opened
    Then traffic flows
"""


def _write_two_area_corpus(tmp_path: Path) -> Path:
    return write_spec_corpus(
        tmp_path / "specs",
        {
            "authentication/signin.feature": _SIGNIN_FEATURE,
            "authentication/session.feature": _SESSION_FEATURE,
            "authentication/invariants.feature": _AUTH_INVARIANTS_FEATURE,
            "networking/tunnels/hole-punching.feature": _TUNNELS_FEATURE,
        },
    )


def test_discover_spec_tasks_groups_units_per_feature_file_with_dotted_display_ids(tmp_path: Path) -> None:
    corpus_root = _write_two_area_corpus(tmp_path)

    tasks = discover_spec_tasks(scan_root=corpus_root, area=None, tag=None, unit_kind=None)

    id_by_display_id = {task.display_id: task.id for task in tasks}
    assert id_by_display_id == {
        "authentication.invariants": "authentication/invariants.feature",
        "authentication.session": "authentication/session.feature",
        "authentication.signin": "authentication/signin.feature",
        "networking.tunnels.hole-punching": "networking/tunnels/hole-punching.feature",
    }


def test_discover_spec_tasks_preserves_corpus_scan_order(tmp_path: Path) -> None:
    corpus_root = _write_two_area_corpus(tmp_path)

    tasks = discover_spec_tasks(scan_root=corpus_root, area=None, tag=None, unit_kind=None)

    # scan_corpus walks folders and files in sorted order; grouping must not reorder.
    assert [task.id for task in tasks] == [
        "authentication/invariants.feature",
        "authentication/session.feature",
        "authentication/signin.feature",
        "networking/tunnels/hole-punching.feature",
    ]


def test_discover_spec_tasks_area_filter_keeps_only_that_subtree(tmp_path: Path) -> None:
    corpus_root = _write_two_area_corpus(tmp_path)

    tasks = discover_spec_tasks(scan_root=corpus_root, area="networking.tunnels", tag=None, unit_kind=None)

    assert [task.id for task in tasks] == ["networking/tunnels/hole-punching.feature"]


def test_discover_spec_tasks_tag_filter_selects_single_unit_task(tmp_path: Path) -> None:
    corpus_root = _write_two_area_corpus(tmp_path)

    tasks = discover_spec_tasks(scan_root=corpus_root, area=None, tag="authentication.used-code", unit_kind=None)

    assert [task.id for task in tasks] == ["authentication/signin.feature"]


def test_discover_spec_tasks_unit_kind_filter_keeps_only_rule_files(tmp_path: Path) -> None:
    corpus_root = _write_two_area_corpus(tmp_path)

    tasks = discover_spec_tasks(scan_root=corpus_root, area=None, tag=None, unit_kind=SpecUnitKind.RULE)

    assert [task.id for task in tasks] == ["authentication/invariants.feature"]


def test_discover_spec_tasks_filters_compose_with_and_semantics(tmp_path: Path) -> None:
    corpus_root = _write_two_area_corpus(tmp_path)

    with pytest.raises(NoSpecUnitsError):
        # The area matches units, and the kind matches units, but no unit matches both.
        discover_spec_tasks(scan_root=corpus_root, area="networking", tag=None, unit_kind=SpecUnitKind.RULE)


def test_discover_spec_tasks_raises_on_corpus_language_violations(tmp_path: Path) -> None:
    corpus_root = write_spec_corpus(
        tmp_path / "specs",
        # An untagged scenario violates the every-unit-carries-an-identity-tag rule.
        {
            "authentication/signin.feature": """Feature: Sign in
  Scenario: Untagged scenario
    When something happens
    Then something is observed
"""
        },
    )

    with pytest.raises(SpecCorpusInvalidError) as exc_info:
        discover_spec_tasks(scan_root=corpus_root, area=None, tag=None, unit_kind=None)

    assert "signin.feature" in str(exc_info.value)


def test_discover_spec_tasks_raises_when_corpus_has_no_units(tmp_path: Path) -> None:
    corpus_root = write_spec_corpus(tmp_path / "specs", {})

    with pytest.raises(NoSpecUnitsError):
        discover_spec_tasks(scan_root=corpus_root, area=None, tag=None, unit_kind=None)


def test_discover_spec_tasks_on_live_minds_corpus(tmp_path: Path) -> None:
    """The live corpus discovers into one well-formed task per feature file."""
    live_corpus_root = Path(__file__).resolve().parents[4] / "apps" / "minds" / "specs"

    tasks = discover_spec_tasks(scan_root=live_corpus_root, area=None, tag=None, unit_kind=None)

    assert len(tasks) > 0
    assert all(task.id.endswith(".feature") for task in tasks)
    assert all(task.display_id is not None and "/" not in task.display_id for task in tasks)
    # The invariants file fans out as a first-class task like any other.
    assert "authentication/invariants.feature" in {task.id for task in tasks}
