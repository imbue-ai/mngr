"""Tests for behavioral-spec corpus scanning: unit extraction and rule validation.

All corpora are synthetic (built under ``tmp_path`` via ``write_spec_corpus``);
nothing here reads the live ``apps/minds/specs/`` corpus.
"""

from pathlib import Path

from imbue.minds.core.behavioral_specs.corpus import scan_corpus
from imbue.minds.core.behavioral_specs.data_types import SpecUnitKind
from imbue.minds.core.behavioral_specs.testing import write_spec_corpus


def test_scan_corpus_extracts_scenario_unit_with_folder_qualified_coordinate(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "authentication/signin.feature": (
                "Feature: Sign-in\n"
                "\n"
                "  @fresh-code @happy-path\n"
                "  Scenario: Opening a fresh login URL signs the user in\n"
                "    Given the user is not signed in\n"
                "    When the user opens the login URL\n"
                "    Then the user is signed in\n"
            ),
        },
    )

    scan = scan_corpus(root)

    assert scan.violations == ()
    assert len(scan.units) == 1
    unit = scan.units[0]
    assert unit.coordinate == "authentication.fresh-code"
    assert unit.kind == SpecUnitKind.SCENARIO
    assert unit.name == "Opening a fresh login URL signs the user in"
    assert unit.file == root / "authentication" / "signin.feature"
    assert unit.line == 4
    assert unit.tags == ("fresh-code", "happy-path")
    assert [(step.keyword, step.text) for step in unit.steps] == [
        ("Given", "the user is not signed in"),
        ("When", "the user opens the login URL"),
        ("Then", "the user is signed in"),
    ]
    assert unit.parent is None


def test_scan_corpus_extracts_outline_rule_and_rule_child_units(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "authentication/invariants.feature": (
                "Feature: Authentication invariants\n"
                "\n"
                "  @missing-code\n"
                "  Scenario Outline: Requests without a code are malformed\n"
                '    When a request is made to "<path>"\n'
                "    Then it is rejected as malformed input\n"
                "\n"
                "    Examples:\n"
                "      | path   |\n"
                "      | /login |\n"
                "\n"
                "  @single-use-codes\n"
                "  Rule: A one-time code grants at most one session, ever\n"
                "    Rationale prose.\n"
                "\n"
                "    @spent-code-refused\n"
                "    Example: A spent code cannot sign anyone in again\n"
                "      Given the login URL has already been used to sign in\n"
                "      Then authentication is refused\n"
            ),
        },
    )

    scan = scan_corpus(root)

    assert scan.violations == ()
    assert [(unit.coordinate, unit.kind, unit.parent) for unit in scan.units] == [
        ("authentication.missing-code", SpecUnitKind.SCENARIO_OUTLINE, None),
        ("authentication.single-use-codes", SpecUnitKind.RULE, None),
        ("authentication.spent-code-refused", SpecUnitKind.SCENARIO, "authentication.single-use-codes"),
    ]
    rule_unit = scan.units[1]
    assert rule_unit.name == "A one-time code grants at most one session, ever"
    assert rule_unit.steps == ()
    assert rule_unit.tags == ("single-use-codes",)


def test_scan_corpus_gives_root_level_files_bare_tag_coordinates(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "invariants.feature": (
                "Feature: Corpus invariants\n"
                "\n"
                "  @single-use-codes\n"
                "  Rule: A one-time code grants at most one session, ever\n"
            ),
        },
    )

    scan = scan_corpus(root)

    assert scan.violations == ()
    assert [unit.coordinate for unit in scan.units] == ["single-use-codes"]


def test_scan_corpus_orders_units_by_file_path_then_document_order(tmp_path: Path) -> None:
    scenario = "  @{tag}\n  Scenario: s\n    Given a thing\n"
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "beta/z.feature": "Feature: F\n" + scenario.format(tag="second"),
            "alpha/b.feature": "Feature: F\n" + scenario.format(tag="first"),
            "alpha/a.feature": ("Feature: F\n" + scenario.format(tag="early") + scenario.format(tag="late")),
        },
    )

    scan = scan_corpus(root)

    assert [unit.coordinate for unit in scan.units] == [
        "alpha.early",
        "alpha.late",
        "alpha.first",
        "beta.second",
    ]


def test_scan_corpus_reports_untagged_unit_and_omits_it_from_records(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "authentication/signin.feature": (
                "Feature: Sign-in\n"
                "\n"
                "  Scenario: No identity tag here\n"
                "    Given a thing\n"
                "\n"
                "  @tagged\n"
                "  Scenario: This one is fine\n"
                "    Given a thing\n"
            ),
        },
    )

    scan = scan_corpus(root)

    assert [unit.coordinate for unit in scan.units] == ["authentication.tagged"]
    assert len(scan.violations) == 1
    violation = scan.violations[0]
    assert violation.file == root / "authentication" / "signin.feature"
    assert violation.line == 3
    assert "at least one tag" in violation.message
    assert "No identity tag here" in violation.message
    assert violation.is_unit_omitted is True
