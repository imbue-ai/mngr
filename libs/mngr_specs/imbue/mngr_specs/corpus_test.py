"""Tests for behavioral-spec corpus scanning: unit extraction and rule validation.

Every corpus here is synthetic, built under ``tmp_path`` via ``write_spec_corpus``.
"""

import json
from pathlib import Path

from inline_snapshot import snapshot

from imbue.mngr_specs.corpus import binding_invariant_coordinates
from imbue.mngr_specs.corpus import scan_corpus
from imbue.mngr_specs.corpus import spec_unit_to_record
from imbue.mngr_specs.data_types import SpecUnit
from imbue.mngr_specs.data_types import SpecUnitKind
from imbue.mngr_specs.testing import write_spec_corpus


def _unit_with_coordinate(units: tuple[SpecUnit, ...], coordinate: str) -> SpecUnit:
    return next(unit for unit in units if unit.coordinate == coordinate)


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


def test_scan_corpus_reports_parse_errors_with_line_and_skips_the_file(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "authentication/broken.feature": (
                'Feature: Broken\n\n  @a-tag\n  Scenario: s\n    Given a\n    """\n    unclosed docstring\n'
            ),
            "authentication/good.feature": ("Feature: Good\n\n  @works\n  Scenario: s\n    Given a\n"),
        },
    )

    scan = scan_corpus(root)

    assert [unit.coordinate for unit in scan.units] == ["authentication.works"]
    assert len(scan.violations) == 1
    violation = scan.violations[0]
    assert violation.file == root / "authentication" / "broken.feature"
    assert violation.line == 8
    assert "unexpected end of file" in violation.message
    assert violation.is_unit_omitted is True
    assert scan.feature_file_count == 2


def test_scan_corpus_reports_non_kebab_case_tags_wherever_they_appear(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "authentication/signin.feature": (
                "@Feature_Tag\n"
                "Feature: Sign-in\n"
                "\n"
                "  @Bad_Identity @ok-auxiliary @badAux\n"
                "  Scenario Outline: s\n"
                "    Given <a>\n"
                "\n"
                "    @Examples_Tag\n"
                "    Examples:\n"
                "      | a |\n"
                "      | 1 |\n"
            ),
        },
    )

    scan = scan_corpus(root)

    # The unit is still representable (its identity tag exists), so it is emitted.
    assert [unit.coordinate for unit in scan.units] == ["authentication.Bad_Identity"]
    offending = sorted((violation.line, violation.message) for violation in scan.violations)
    assert len(offending) == 4
    assert all("kebab-case" in message for _, message in offending)
    assert [line for line, _ in offending] == [1, 4, 4, 8]
    assert "Feature_Tag" in offending[0][1]
    assert "Bad_Identity" in offending[1][1]
    assert "badAux" in offending[2][1]
    assert "Examples_Tag" in offending[3][1]
    assert all(violation.is_unit_omitted is False for violation in scan.violations)


def test_scan_corpus_reports_non_kebab_folder_and_file_names_and_unexpected_files(tmp_path: Path) -> None:
    valid_feature = "Feature: F\n\n  @a-tag\n  Scenario: s\n    Given a\n"
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "Bad_Folder/fine.feature": valid_feature,
            "good-folder/Bad_Name.feature": valid_feature.replace("@a-tag", "@b-tag"),
            "good-folder/Bad_Sidecar.md": "prose\n",
            "good-folder/notes.txt": "not a corpus artifact\n",
            ".hidden-dir/ignored.feature": "not even parsed {",
            "good-folder/.DS_Store": "binary junk",
        },
    )

    scan = scan_corpus(root)

    messages = sorted(violation.message for violation in scan.violations)
    assert len(messages) == 5
    assert any("folder name 'Bad_Folder' is not kebab-case" in message for message in messages)
    assert any("file basename 'Bad_Name' is not kebab-case" in message for message in messages)
    assert any("file basename 'Bad_Sidecar' is not kebab-case" in message for message in messages)
    # Bad_Sidecar.md also dangles (no Bad_Sidecar.feature): reported separately.
    assert any("no matching" in message and "Bad_Sidecar" in message for message in messages)
    assert any("notes.txt" in message and "only .feature and .md files" in message for message in messages)
    # Hidden files and folders are tooling artifacts, not corpus content.
    assert not any(".DS_Store" in message or "hidden" in message for message in messages)
    assert {unit.coordinate for unit in scan.units} == {"Bad_Folder.a-tag", "good-folder.b-tag"}


def test_scan_corpus_enforces_reserved_names_for_overview_and_invariants(tmp_path: Path) -> None:
    valid_feature = "Feature: F\n\n  @a-tag\n  Scenario: s\n    Given a\n"
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "overview.md": "corpus context, no matching feature needed\n",
            "authentication/overview.md": "folder context\n",
            "authentication/overview.feature": valid_feature,
            "authentication/invariants.feature": valid_feature.replace("@a-tag", "@b-tag"),
            "authentication/invariants.md": "sidecar of invariants.feature\n",
            "networking/invariants.md": "dangling: no invariants.feature here\n",
            "networking/session.feature": valid_feature.replace("@a-tag", "@c-tag"),
        },
    )

    scan = scan_corpus(root)

    messages = sorted(violation.message for violation in scan.violations)
    assert len(messages) == 2
    assert any("overview" in message and "reserved" in message for message in messages)
    assert any("invariants.md" in message and "no matching" in message for message in messages)
    overview_violation = next(v for v in scan.violations if "reserved" in v.message)
    assert overview_violation.file == root / "authentication" / "overview.feature"


def test_scan_corpus_rejects_language_headers_even_for_english(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "signin.feature": ("# language: en\nFeature: F\n\n  @a-tag\n  Scenario: s\n    Given a\n"),
        },
    )

    scan = scan_corpus(root)

    assert len(scan.violations) == 1
    violation = scan.violations[0]
    assert violation.line == 1
    assert "# language:" in violation.message
    assert violation.is_unit_omitted is False
    # The file still parses as English, so its units remain representable.
    assert [unit.coordinate for unit in scan.units] == ["a-tag"]


def test_scan_corpus_rejects_non_english_gherkin_dialects(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "connexion.feature": (
                "# language: fr\nFonctionnalité: Connexion\n\n  @un-tag\n  Scénario: ouverture\n    Soit une chose\n"
            ),
        },
    )

    scan = scan_corpus(root)

    assert any("# language:" in violation.message and violation.line == 1 for violation in scan.violations)
    assert any("English" in violation.message and "'fr'" in violation.message for violation in scan.violations)


def test_scan_corpus_rejects_feature_files_without_a_feature(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "empty.feature": "",
            "comments-only.feature": "# nothing but a comment\n",
        },
    )

    scan = scan_corpus(root)

    assert scan.units == ()
    assert len(scan.violations) == 2
    assert all("exactly one Feature" in violation.message for violation in scan.violations)
    assert {violation.file.name for violation in scan.violations} == {"empty.feature", "comments-only.feature"}


def test_scan_corpus_rejects_english_synonym_keywords_outside_the_language_construct_list(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "synonyms.feature": (
                "Ability: F\n"
                "\n"
                "  @a-tag\n"
                "  Scenario Template: s\n"
                "    Given <a>\n"
                "\n"
                "    Scenarios:\n"
                "      | a |\n"
                "      | 1 |\n"
                "\n"
                "  @b-tag\n"
                "  Scenario: t\n"
                "    * a freeform step\n"
            ),
        },
    )

    scan = scan_corpus(root)

    messages = sorted(violation.message for violation in scan.violations)
    assert len(messages) == 4
    assert any("'Ability'" in message and "'Feature'" in message for message in messages)
    assert any("'Scenario Template'" in message for message in messages)
    assert any("'Scenarios'" in message and "'Examples'" in message for message in messages)
    assert any("'*'" in message for message in messages)
    # Units are still representable; a Scenario Template is an outline.
    assert [(unit.coordinate, unit.kind) for unit in scan.units] == [
        ("a-tag", SpecUnitKind.SCENARIO_OUTLINE),
        ("b-tag", SpecUnitKind.SCENARIO),
    ]


def test_scan_corpus_rejects_duplicate_coordinate_claims_within_a_folder(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            # a.feature: the first claim of auth.dup-tag (unit identity).
            "auth/a.feature": ("Feature: A\n\n  @dup-tag\n  Scenario: first claimant\n    Given a\n"),
            # b.feature: a Feature-block tag re-claims auth.dup-tag; an
            # auxiliary tag repeating dup-tag is exempt from uniqueness; an
            # Examples tag re-claims auth.uniq-tag (already the identity of
            # the unit right above it).
            "auth/b.feature": (
                "@dup-tag\n"
                "Feature: B\n"
                "\n"
                "  @uniq-tag @dup-tag\n"
                "  Scenario Outline: outline\n"
                "    Given <a>\n"
                "\n"
                "    @uniq-tag\n"
                "    Examples:\n"
                "      | a |\n"
                "      | 1 |\n"
            ),
            # Same raw tag in a different folder claims a different coordinate.
            "other/c.feature": ("Feature: C\n\n  @dup-tag\n  Scenario: unrelated folder\n    Given a\n"),
        },
    )

    scan = scan_corpus(root)

    duplicate_messages = sorted(
        (violation.file.name, violation.line, violation.message)
        for violation in scan.violations
        if "claimed" in violation.message
    )
    assert len(duplicate_messages) == 2
    assert duplicate_messages[0][0] == "b.feature"
    assert duplicate_messages[0][1] == 1
    assert "'auth.dup-tag'" in duplicate_messages[0][2]
    assert "a.feature:3" in duplicate_messages[0][2]
    assert duplicate_messages[1][0] == "b.feature"
    assert duplicate_messages[1][1] == 8
    assert "'auth.uniq-tag'" in duplicate_messages[1][2]
    assert "b.feature:4" in duplicate_messages[1][2]
    # No other violations: the auxiliary repeat and the cross-folder repeat are fine.
    assert len(scan.violations) == 2


def test_scan_corpus_accepts_a_rich_fully_valid_corpus(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "overview.md": "corpus-wide context\n",
            "invariants.feature": (
                "Feature: Corpus invariants\n"
                "\n"
                "  @no-plaintext-secrets\n"
                "  Rule: Secrets never appear in plain text\n"
                "    Rationale prose.\n"
            ),
            "authentication/overview.md": "authentication context\n",
            "authentication/signin.feature": (
                "@signin-surface\n"
                "Feature: Sign-in with a one-time login code\n"
                "  Free prose description.\n"
                "\n"
                "  Background:\n"
                "    Given a running desktop client\n"
                "\n"
                "  @fresh-code\n"
                "  Scenario: Opening a fresh login URL signs the user in\n"
                "    Given the user is not signed in\n"
                "    When the user opens the login URL with payload:\n"
                '      """\n'
                "      any doc string\n"
                '      """\n'
                "    Then the user is signed in\n"
                "    And a table is fine:\n"
                "      | key | value |\n"
                "      | a   | 1     |\n"
                "    But nothing else happens\n"
                "\n"
                "  @missing-code\n"
                "  Scenario Outline: Requests without a code are malformed\n"
                '    When a request is made to "<path>"\n'
                "    Then it is rejected\n"
                "\n"
                "    @missing-code-paths\n"
                "    Examples:\n"
                "      | path   |\n"
                "      | /login |\n"
            ),
            "authentication/signin.md": "sidecar prose for signin.feature\n",
        },
    )

    scan = scan_corpus(root)

    assert scan.violations == ()
    assert scan.feature_file_count == 2
    assert [unit.coordinate for unit in scan.units] == [
        "authentication.fresh-code",
        "authentication.missing-code",
        "no-plaintext-secrets",
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


def test_spec_unit_to_record_serializes_stable_field_order_and_kind_spelling(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "authentication/session.feature": (
                "Feature: Session\n"
                "\n"
                "  @installation-bound\n"
                "  Rule: Only local tokens are accepted\n"
                "\n"
                "    @spent-code @edge-case\n"
                "    Scenario Outline: Spent codes are refused\n"
                '      When anyone presents "<code>"\n'
                "      Then authentication is refused\n"
                "\n"
                "      Examples:\n"
                "        | code |\n"
                "        | c1   |\n"
            ),
        },
    )

    scan = scan_corpus(root)

    child_record = spec_unit_to_record(scan.units[1], scan.units, root)
    rendered = json.dumps(child_record, ensure_ascii=False).replace(str(root), "<root>")
    assert rendered == snapshot(
        '{"coordinate": "authentication.spent-code", "kind": "scenario-outline", "name": "Spent codes are refused", "file": "<root>/authentication/session.feature", "line": 7, "tags": ["spent-code", "edge-case"], "steps": [{"keyword": "When", "text": "anyone presents \\"<code>\\""}, {"keyword": "Then", "text": "authentication is refused"}], "parent": "authentication.installation-bound", "invariants": ["authentication.installation-bound"]}'
    )
    rule_record = spec_unit_to_record(scan.units[0], scan.units, root)
    assert rule_record["kind"] == "rule"
    assert rule_record["steps"] == []
    assert rule_record["parent"] is None
    # The lone Rule has no other Rule binding it, so its invariants list is empty.
    assert rule_record["invariants"] == []


def test_binding_invariants_file_scoped_rule_binds_same_file_units_only(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "authentication/signin.feature": (
                "Feature: Sign-in\n"
                "\n"
                "  @fresh-code\n"
                "  Scenario: fresh\n"
                "    Given a thing\n"
                "\n"
                "  @installation-bound\n"
                "  Rule: Only local tokens are accepted\n"
            ),
            "authentication/session.feature": (
                "Feature: Session\n\n  @other-flow\n  Scenario: other\n    Given a thing\n"
            ),
        },
    )

    scan = scan_corpus(root)

    assert scan.violations == ()
    fresh_code = _unit_with_coordinate(scan.units, "authentication.fresh-code")
    other_flow = _unit_with_coordinate(scan.units, "authentication.other-flow")
    # The file-scoped Rule binds its file-mate but not a unit in a sibling file of the same folder.
    assert binding_invariant_coordinates(fresh_code, scan.units, root) == ("authentication.installation-bound",)
    assert binding_invariant_coordinates(other_flow, scan.units, root) == ()


def test_binding_invariants_folder_invariants_rule_binds_folder_and_nested_subfolders(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "authentication/invariants.feature": (
                "Feature: Authentication invariants\n\n  @single-use-codes\n  Rule: single use\n"
            ),
            "authentication/signin.feature": (
                "Feature: Sign-in\n\n  @fresh-code\n  Scenario: fresh\n    Given a thing\n"
            ),
            "authentication/oauth/flow.feature": (
                "Feature: OAuth\n\n  @nested-flow\n  Scenario: nested\n    Given a thing\n"
            ),
        },
    )

    scan = scan_corpus(root)

    assert scan.violations == ()
    fresh_code = _unit_with_coordinate(scan.units, "authentication.fresh-code")
    nested_flow = _unit_with_coordinate(scan.units, "authentication.oauth.nested-flow")
    assert binding_invariant_coordinates(fresh_code, scan.units, root) == ("authentication.single-use-codes",)
    assert binding_invariant_coordinates(nested_flow, scan.units, root) == ("authentication.single-use-codes",)


def test_binding_invariants_corpus_root_invariants_rule_binds_every_unit(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "invariants.feature": ("Feature: Corpus invariants\n\n  @global-rule\n  Rule: global\n"),
            "authentication/signin.feature": (
                "Feature: Sign-in\n\n  @fresh-code\n  Scenario: fresh\n    Given a thing\n"
            ),
            "networking/tunnels/hole-punching.feature": (
                "Feature: Tunnels\n\n  @deep-flow\n  Scenario: deep\n    Given a thing\n"
            ),
        },
    )

    scan = scan_corpus(root)

    assert scan.violations == ()
    fresh_code = _unit_with_coordinate(scan.units, "authentication.fresh-code")
    deep_flow = _unit_with_coordinate(scan.units, "networking.tunnels.deep-flow")
    assert binding_invariant_coordinates(fresh_code, scan.units, root) == ("global-rule",)
    assert binding_invariant_coordinates(deep_flow, scan.units, root) == ("global-rule",)


def test_binding_invariants_rule_never_binds_itself(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "authentication/invariants.feature": (
                "Feature: Authentication invariants\n\n  @single-use-codes\n  Rule: single use\n"
            ),
        },
    )

    scan = scan_corpus(root)

    assert scan.violations == ()
    single_use = _unit_with_coordinate(scan.units, "authentication.single-use-codes")
    assert binding_invariant_coordinates(single_use, scan.units, root) == ()


def test_binding_invariants_rule_is_bound_by_ancestor_invariants_but_not_its_own_descendants(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "invariants.feature": ("Feature: Corpus invariants\n\n  @global-rule\n  Rule: global\n"),
            "authentication/invariants.feature": (
                "Feature: Authentication invariants\n\n  @auth-rule\n  Rule: auth\n"
            ),
        },
    )

    scan = scan_corpus(root)

    assert scan.violations == ()
    global_rule = _unit_with_coordinate(scan.units, "global-rule")
    auth_rule = _unit_with_coordinate(scan.units, "authentication.auth-rule")
    # The ancestor invariants-file Rule binds the nested Rule, but not the reverse.
    assert binding_invariant_coordinates(auth_rule, scan.units, root) == ("global-rule",)
    assert binding_invariant_coordinates(global_rule, scan.units, root) == ()


def test_binding_invariants_illustrating_child_lists_its_parent_rule(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "authentication/session.feature": (
                "Feature: Session\n"
                "\n"
                "  @installation-bound\n"
                "  Rule: Only local tokens are accepted\n"
                "\n"
                "    @foreign-token\n"
                "    Example: A foreign token reads as signed out\n"
                "      Given a session token minted under another data directory\n"
                "      Then the user is treated as signed out\n"
            ),
        },
    )

    scan = scan_corpus(root)

    assert scan.violations == ()
    foreign_token = _unit_with_coordinate(scan.units, "authentication.foreign-token")
    assert foreign_token.parent == "authentication.installation-bound"
    # The child is in the same file as its Rule, so its binding invariants include that parent.
    assert binding_invariant_coordinates(foreign_token, scan.units, root) == ("authentication.installation-bound",)


def test_binding_invariants_unit_with_no_binding_rules_gets_empty_tuple(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "authentication/signin.feature": (
                "Feature: Sign-in\n\n  @fresh-code\n  Scenario: fresh\n    Given a thing\n"
            ),
        },
    )

    scan = scan_corpus(root)

    assert scan.violations == ()
    assert binding_invariant_coordinates(scan.units[0], scan.units, root) == ()


def test_binding_invariants_ordering_follows_corpus_scan_order(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "invariants.feature": (
                "Feature: Corpus invariants\n\n  @root-first\n  Rule: r1\n\n  @root-second\n  Rule: r2\n"
            ),
            "authentication/invariants.feature": (
                "Feature: Authentication invariants\n\n  @auth-first\n  Rule: a1\n\n  @auth-second\n  Rule: a2\n"
            ),
            "authentication/signin.feature": (
                "Feature: Sign-in\n"
                "\n"
                "  @fresh-code\n"
                "  Scenario: fresh\n"
                "    Given a thing\n"
                "\n"
                "  @signin-local\n"
                "  Rule: file-scoped\n"
            ),
        },
    )

    scan = scan_corpus(root)

    assert scan.violations == ()
    fresh_code = _unit_with_coordinate(scan.units, "authentication.fresh-code")
    # Binding Rules appear in CorpusScan.units order: sorted file path, then document order. The file-scoped
    # Rule (signin-local) is interleaved by its scan position, not grouped separately from the invariants files.
    assert binding_invariant_coordinates(fresh_code, scan.units, root) == (
        "authentication.auth-first",
        "authentication.auth-second",
        "authentication.signin-local",
        "root-first",
        "root-second",
    )


def test_scan_corpus_rejects_examples_blocks_under_a_plain_scenario(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "signin.feature": (
                "Feature: F\n"
                "\n"
                "  @a-tag\n"
                "  Scenario: not an outline\n"
                "    Given <a>\n"
                "\n"
                "    Examples:\n"
                "      | a |\n"
                "      | 1 |\n"
            ),
        },
    )

    scan = scan_corpus(root)

    assert len(scan.violations) == 1
    violation = scan.violations[0]
    assert violation.line == 7
    assert "Examples" in violation.message
    assert "Scenario Outline" in violation.message
    assert [(unit.coordinate, unit.kind) for unit in scan.units] == [("a-tag", SpecUnitKind.SCENARIO)]
