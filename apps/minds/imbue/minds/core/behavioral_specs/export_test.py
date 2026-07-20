"""Tests for the enriched behavioral-spec export.

Corpora are synthetic (built under ``tmp_path`` via ``write_spec_corpus``).
"""

from pathlib import Path

from imbue.minds.core.behavioral_specs.data_types import ApplicableRuleScope
from imbue.minds.core.behavioral_specs.data_types import SpecProseFileKind
from imbue.minds.core.behavioral_specs.data_types import SpecUnitKind
from imbue.minds.core.behavioral_specs.export import export_corpus
from imbue.minds.core.behavioral_specs.export import exported_unit_to_record
from imbue.minds.core.behavioral_specs.export import exported_unit_to_tmr_task_packet
from imbue.minds.core.behavioral_specs.testing import write_spec_corpus


def _write_nested_corpus(root: Path) -> Path:
    """A corpus exercising backgrounds, outlines, descriptions, prose, and every rule scope."""
    return write_spec_corpus(
        root,
        {
            "overview.md": "corpus-wide context\n",
            "invariants.feature": (
                "Feature: Corpus invariants\n"
                "\n"
                "  @never-leak\n"
                "  Rule: Sessions never leak across users\n"
                "    Rationale prose for the corpus invariant.\n"
            ),
            "authentication/overview.md": "authentication context\n",
            "authentication/invariants.feature": (
                "Feature: Authentication invariants\n"
                "\n"
                "  @single-use-codes\n"
                "  Rule: A one-time code grants at most one session, ever\n"
                "    Rationale prose for the folder invariant.\n"
                "\n"
                "    @spent-code-refused\n"
                "    Example: A spent code cannot sign anyone in again\n"
                "      Given the login URL has already been used\n"
                "      Then authentication is refused\n"
            ),
            "authentication/signin.feature": (
                "Feature: Sign-in with a one-time login code\n"
                "  Feature description prose.\n"
                "\n"
                "  Background:\n"
                "    Given a running desktop client\n"
                "    And its terminal printed a login URL\n"
                "\n"
                "  @fresh-code\n"
                "  Scenario: Opening a fresh login URL signs the user in\n"
                "    Scenario description prose.\n"
                "    Given the user is not signed in\n"
                "    When the user opens the login URL\n"
                "    Then the user is signed in\n"
                "\n"
                "  @missing-code\n"
                "  Scenario Outline: Requests without a code are malformed\n"
                '    When a request is made to "<path>"\n'
                "    Then it is rejected\n"
                "\n"
                "    Examples:\n"
                "      | path          |\n"
                "      | /login        |\n"
                "      | /authenticate |\n"
                "\n"
                "  @installation-bound\n"
                "  Rule: Only tokens minted by this installation are accepted\n"
                "    File-scoped rule prose.\n"
            ),
            "authentication/signin.md": "sidecar prose for signin.feature\n",
        },
    )


def test_export_folds_background_into_effective_steps_and_keeps_raw_steps(tmp_path: Path) -> None:
    root = _write_nested_corpus(tmp_path / "specs")

    export = export_corpus(root)

    assert export.violations == ()
    fresh_code = next(unit for unit in export.units if unit.coordinate == "authentication.fresh-code")
    assert [(step.keyword, step.text) for step in fresh_code.raw_steps] == [
        ("Given", "the user is not signed in"),
        ("When", "the user opens the login URL"),
        ("Then", "the user is signed in"),
    ]
    assert [(step.keyword, step.text) for step in fresh_code.effective_steps] == [
        ("Given", "a running desktop client"),
        ("And", "its terminal printed a login URL"),
        ("Given", "the user is not signed in"),
        ("When", "the user opens the login URL"),
        ("Then", "the user is signed in"),
    ]


def test_export_carries_descriptions_feature_and_prose(tmp_path: Path) -> None:
    root = _write_nested_corpus(tmp_path / "specs")

    export = export_corpus(root)

    fresh_code = next(unit for unit in export.units if unit.coordinate == "authentication.fresh-code")
    assert fresh_code.description == "Scenario description prose."
    assert fresh_code.feature_name == "Sign-in with a one-time login code"
    assert fresh_code.feature_description == "Feature description prose."
    assert [(prose.kind, prose.path.name, prose.content) for prose in fresh_code.prose] == [
        (SpecProseFileKind.OVERVIEW, "overview.md", "corpus-wide context\n"),
        (SpecProseFileKind.OVERVIEW, "overview.md", "authentication context\n"),
        (SpecProseFileKind.SIDECAR, "signin.md", "sidecar prose for signin.feature\n"),
    ]
    assert fresh_code.prose[0].path == root / "overview.md"
    assert fresh_code.prose[1].path == root / "authentication" / "overview.md"


def test_export_carries_examples_rows_for_scenario_outlines(tmp_path: Path) -> None:
    root = _write_nested_corpus(tmp_path / "specs")

    export = export_corpus(root)

    outline = next(unit for unit in export.units if unit.coordinate == "authentication.missing-code")
    assert outline.kind == SpecUnitKind.SCENARIO_OUTLINE
    assert len(outline.examples) == 1
    examples = outline.examples[0]
    assert examples.header == ("path",)
    assert examples.rows == (("/login",), ("/authenticate",))


def test_export_resolves_applicable_rules_root_then_folder_then_file(tmp_path: Path) -> None:
    root = _write_nested_corpus(tmp_path / "specs")

    export = export_corpus(root)

    fresh_code = next(unit for unit in export.units if unit.coordinate == "authentication.fresh-code")
    assert [(rule.coordinate, rule.scope) for rule in fresh_code.applicable_rules] == [
        ("never-leak", ApplicableRuleScope.CORPUS),
        ("authentication.single-use-codes", ApplicableRuleScope.FOLDER),
        ("authentication.installation-bound", ApplicableRuleScope.FILE),
    ]
    folder_rule = fresh_code.applicable_rules[1]
    assert folder_rule.name == "A one-time code grants at most one session, ever"
    assert folder_rule.description == "Rationale prose for the folder invariant."
    assert folder_rule.file == root / "authentication" / "invariants.feature"


def test_export_does_not_list_a_rule_as_its_own_applicable_rule(tmp_path: Path) -> None:
    root = _write_nested_corpus(tmp_path / "specs")

    export = export_corpus(root)

    folder_rule = next(unit for unit in export.units if unit.coordinate == "authentication.single-use-codes")
    assert [rule.coordinate for rule in folder_rule.applicable_rules] == ["never-leak"]
    corpus_rule = next(unit for unit in export.units if unit.coordinate == "never-leak")
    assert corpus_rule.applicable_rules == ()


def test_export_links_rule_children_to_the_enclosing_rule(tmp_path: Path) -> None:
    root = _write_nested_corpus(tmp_path / "specs")

    export = export_corpus(root)

    child = next(unit for unit in export.units if unit.coordinate == "authentication.spent-code-refused")
    assert child.parent == "authentication.single-use-codes"
    assert child.rule is not None
    assert child.rule.coordinate == "authentication.single-use-codes"
    assert child.rule.name == "A one-time code grants at most one session, ever"
    assert child.rule.description == "Rationale prose for the folder invariant."
    # The illustrating child is bound by the corpus invariant and by the folder invariant it illustrates.
    assert [rule.coordinate for rule in child.applicable_rules] == ["never-leak", "authentication.single-use-codes"]


def test_export_folds_rule_background_into_rule_children(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "networking/tunnels.feature": (
                "Feature: Tunnels\n"
                "\n"
                "  Background:\n"
                "    Given a running forward server\n"
                "\n"
                "  @no-tls\n"
                "  Rule: Tunnel endpoints never terminate TLS\n"
                "\n"
                "    Background:\n"
                "      Given a connected tunnel\n"
                "\n"
                "    @plain-http\n"
                "    Example: Plain HTTP passes through\n"
                "      When a plain HTTP request crosses the tunnel\n"
                "      Then it is forwarded untouched\n"
            ),
        },
    )

    export = export_corpus(root)

    assert export.violations == ()
    child = next(unit for unit in export.units if unit.coordinate == "networking.plain-http")
    assert [(step.keyword, step.text) for step in child.effective_steps] == [
        ("Given", "a running forward server"),
        ("Given", "a connected tunnel"),
        ("When", "a plain HTTP request crosses the tunnel"),
        ("Then", "it is forwarded untouched"),
    ]


def test_export_scopes_folder_invariants_to_their_subtree_only(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "invariants.feature": ("Feature: Corpus invariants\n\n  @corpus-rule\n  Rule: Holds everywhere\n"),
            "alpha/invariants.feature": ("Feature: Alpha invariants\n\n  @alpha-rule\n  Rule: Holds under alpha\n"),
            "alpha/beta/invariants.feature": ("Feature: Beta invariants\n\n  @beta-rule\n  Rule: Holds under beta\n"),
            "alpha/beta/deep.feature": "Feature: Deep\n\n  @deep-thing\n  Scenario: s\n    Given a\n",
            "alpha/shallow.feature": "Feature: Shallow\n\n  @shallow-thing\n  Scenario: s\n    Given a\n",
            "gamma/other.feature": "Feature: Other\n\n  @other-thing\n  Scenario: s\n    Given a\n",
        },
    )

    export = export_corpus(root)

    assert export.violations == ()
    by_coordinate = {unit.coordinate: unit for unit in export.units}
    assert [rule.coordinate for rule in by_coordinate["alpha.beta.deep-thing"].applicable_rules] == [
        "corpus-rule",
        "alpha.alpha-rule",
        "alpha.beta.beta-rule",
    ]
    assert [rule.coordinate for rule in by_coordinate["alpha.shallow-thing"].applicable_rules] == [
        "corpus-rule",
        "alpha.alpha-rule",
    ]
    assert [rule.coordinate for rule in by_coordinate["gamma.other-thing"].applicable_rules] == ["corpus-rule"]


def test_exported_unit_record_shape(tmp_path: Path) -> None:
    root = _write_nested_corpus(tmp_path / "specs")

    export = export_corpus(root)
    fresh_code = next(unit for unit in export.units if unit.coordinate == "authentication.fresh-code")
    record = exported_unit_to_record(fresh_code)

    assert list(record.keys()) == [
        "coordinate",
        "kind",
        "name",
        "file",
        "line",
        "tags",
        "parent",
        "description",
        "raw_steps",
        "effective_steps",
        "examples",
        "feature",
        "rule",
        "prose",
        "applicable_rules",
    ]
    assert record["coordinate"] == "authentication.fresh-code"
    assert record["kind"] == "scenario"
    assert record["file"] == str(root / "authentication" / "signin.feature")
    assert record["rule"] is None
    assert record["feature"] == {
        "name": "Sign-in with a one-time login code",
        "description": "Feature description prose.",
    }
    assert record["prose"][0]["kind"] == "overview"
    assert record["applicable_rules"][0] == {
        "coordinate": "never-leak",
        "name": "Sessions never leak across users",
        "description": "Rationale prose for the corpus invariant.",
        "file": str(root / "invariants.feature"),
        "scope": "corpus",
    }

    outline = next(unit for unit in export.units if unit.coordinate == "authentication.missing-code")
    outline_record = exported_unit_to_record(outline)
    assert outline_record["examples"] == [{"line": 20, "header": ["path"], "rows": [["/login"], ["/authenticate"]]}]


def test_tmr_task_packet_carries_coordinate_ids_and_export_context(tmp_path: Path) -> None:
    root = _write_nested_corpus(tmp_path / "specs")

    export = export_corpus(root)
    fresh_code = next(unit for unit in export.units if unit.coordinate == "authentication.fresh-code")
    packet = exported_unit_to_tmr_task_packet(fresh_code)

    assert packet["schema_version"] == 1
    assert packet["id"] == "authentication.fresh-code"
    assert packet["display_id"] == "authentication-fresh-code"
    assert packet["kind"] == "scenario"
    assert packet["context"]["coordinate"] == "authentication.fresh-code"
    assert packet["context"]["effective_steps"][0]["text"] == "a running desktop client"


def test_export_passes_violations_through_and_still_exports_representable_units(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "good.feature": "Feature: G\n\n  @works\n  Scenario: s\n    Given a\n",
            "untagged.feature": "Feature: U\n\n  Scenario: no identity\n    Given a\n",
        },
    )

    export = export_corpus(root)

    assert [unit.coordinate for unit in export.units] == ["works"]
    assert len(export.violations) == 1
    assert export.violations[0].is_unit_omitted is True
