"""Tests for the ``mngr specs`` CLI group.

Every corpus is synthetic, built under ``tmp_path`` via ``write_spec_corpus``
and pointed at with ``--root``; nothing here touches a real corpus.
"""

import json
from pathlib import Path

from click.testing import CliRunner

from imbue.mngr_specs.cli import specs
from imbue.mngr_specs.testing import write_spec_corpus

_VALID_CORPUS = {
    "overview.md": "corpus context\n",
    "authentication/signin.feature": (
        "Feature: Sign-in\n"
        "\n"
        "  @fresh-code\n"
        "  Scenario: Opening a fresh login URL signs the user in\n"
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
        "      | path   |\n"
        "      | /login |\n"
        "\n"
        "  @installation-bound\n"
        "  Rule: Only tokens minted by this installation are accepted\n"
        "\n"
        "    @foreign-token\n"
        "    Example: A foreign token reads as signed out\n"
        "      Given a session token minted under another data directory\n"
        "      Then the user is treated as signed out\n"
    ),
}


def test_specs_group_exposes_validate_list_and_matrix() -> None:
    result = CliRunner().invoke(specs, ["--help"])

    assert result.exit_code == 0, result.output
    assert "validate" in result.output
    assert "list" in result.output
    assert "matrix" in result.output
    assert "query" not in result.output


def test_specs_validate_requires_the_root_option() -> None:
    result = CliRunner().invoke(specs, ["validate"])

    # --root is required: click rejects the invocation before any scanning.
    assert result.exit_code != 0
    assert "--root" in result.output


def test_specs_validate_reports_success_concisely_on_a_valid_corpus(tmp_path: Path) -> None:
    root = write_spec_corpus(tmp_path / "specs", _VALID_CORPUS)

    result = CliRunner().invoke(specs, ["validate", "--root", str(root)])

    assert result.exit_code == 0, result.output
    assert result.stdout == f"OK: 4 units across 1 feature file(s) under {root}\n"


def test_specs_validate_lists_every_violation_and_exits_nonzero(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "authentication/signin.feature": (
                "Feature: Sign-in\n"
                "\n"
                "  Scenario: untagged\n"
                "    Given a\n"
                "\n"
                "  @Bad_Tag\n"
                "  Scenario: badly tagged\n"
                "    Given a\n"
            ),
            "dangling.md": "no matching feature\n",
        },
    )

    result = CliRunner().invoke(specs, ["validate", "--root", str(root)])

    assert result.exit_code == 1
    stdout_lines = result.stdout.splitlines()
    assert len(stdout_lines) == 3
    assert stdout_lines[0].startswith(f"{root}/dangling.md: ")
    assert "no matching" in stdout_lines[0]
    assert stdout_lines[1].startswith(f"{root}/authentication/signin.feature:3: ")
    assert "at least one tag" in stdout_lines[1]
    assert stdout_lines[2].startswith(f"{root}/authentication/signin.feature:6: ")
    assert "kebab-case" in stdout_lines[2]
    assert "3 violation(s)" in result.stderr


def test_specs_validate_rejects_a_missing_corpus_root(tmp_path: Path) -> None:
    missing_root = tmp_path / "not-there"

    result = CliRunner().invoke(specs, ["validate", "--root", str(missing_root)])

    assert result.exit_code == 1
    assert "not a directory" in result.stderr
    assert "--root" in result.stderr


def test_specs_list_emits_one_json_record_per_unit_and_nothing_else_on_stdout(tmp_path: Path) -> None:
    root = write_spec_corpus(tmp_path / "specs", _VALID_CORPUS)

    result = CliRunner().invoke(specs, ["list", "--root", str(root)])

    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert [(record["coordinate"], record["kind"], record["parent"]) for record in records] == [
        ("authentication.fresh-code", "scenario", None),
        ("authentication.missing-code", "scenario-outline", None),
        ("authentication.installation-bound", "rule", None),
        ("authentication.foreign-token", "scenario", "authentication.installation-bound"),
    ]
    fresh_code = records[0]
    assert fresh_code["name"] == "Opening a fresh login URL signs the user in"
    assert fresh_code["file"] == str(root / "authentication" / "signin.feature")
    assert fresh_code["line"] == 4
    assert fresh_code["tags"] == ["fresh-code"]
    assert fresh_code["steps"][0] == {"keyword": "Given", "text": "the user is not signed in"}
    # The lone Rule in this file binds its file-mates but not itself.
    assert fresh_code["invariants"] == ["authentication.installation-bound"]
    installation_bound = records[2]
    assert installation_bound["coordinate"] == "authentication.installation-bound"
    assert installation_bound["invariants"] == []


def test_specs_list_filters_by_unit_kind(tmp_path: Path) -> None:
    root = write_spec_corpus(tmp_path / "specs", _VALID_CORPUS)

    result = CliRunner().invoke(specs, ["list", "--root", str(root), "--unit", "rule"])

    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert [record["coordinate"] for record in records] == ["authentication.installation-bound"]


def test_specs_list_reports_omitted_units_on_stderr_and_exits_nonzero(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "good.feature": "Feature: G\n\n  @works\n  Scenario: s\n    Given a\n",
            "broken.feature": ('Feature: B\n\n  @a-tag\n  Scenario: s\n    Given a\n    """\n    unclosed\n'),
            "untagged.feature": "Feature: U\n\n  Scenario: no identity\n    Given a\n",
        },
    )

    result = CliRunner().invoke(specs, ["list", "--root", str(root)])

    assert result.exit_code == 1
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert [record["coordinate"] for record in records] == ["works"]
    assert "broken.feature" in result.stderr
    assert "untagged.feature" in result.stderr
    assert "incomplete" in result.stderr


def test_specs_list_filters_by_exact_tag_or_coordinate(tmp_path: Path) -> None:
    root = write_spec_corpus(tmp_path / "specs", _VALID_CORPUS)
    runner = CliRunner()

    by_raw_tag = runner.invoke(specs, ["list", "--root", str(root), "--tag", "fresh-code"])
    by_coordinate = runner.invoke(specs, ["list", "--root", str(root), "--tag", "authentication.fresh-code"])
    with_sigil = runner.invoke(specs, ["list", "--root", str(root), "--tag", "@fresh-code"])
    no_match = runner.invoke(specs, ["list", "--root", str(root), "--tag", "fresh"])

    for result in (by_raw_tag, by_coordinate, with_sigil):
        assert result.exit_code == 0, result.output
        records = [json.loads(line) for line in result.stdout.splitlines()]
        assert [record["coordinate"] for record in records] == ["authentication.fresh-code"]
    assert no_match.exit_code == 0
    assert no_match.stdout == ""


def test_specs_list_tag_filter_emits_every_unit_sharing_an_auxiliary_tag(tmp_path: Path) -> None:
    root = write_spec_corpus(
        tmp_path / "specs",
        {
            "authentication/signin.feature": (
                "Feature: Sign-in\n\n  @fresh-code @happy-path\n  Scenario: fresh\n    Given a thing\n"
            ),
            "authentication/session.feature": (
                "Feature: Session\n\n  @survives-restart @happy-path\n  Scenario: restart\n    Given a thing\n"
            ),
        },
    )

    result = CliRunner().invoke(specs, ["list", "--root", str(root), "--tag", "happy-path"])

    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in result.stdout.splitlines()]
    # Auxiliary tags may repeat across units, so a --tag match is not necessarily unique.
    assert [record["coordinate"] for record in records] == [
        "authentication.survives-restart",
        "authentication.fresh-code",
    ]


def test_specs_list_filters_by_case_insensitive_name_substring(tmp_path: Path) -> None:
    root = write_spec_corpus(tmp_path / "specs", _VALID_CORPUS)

    result = CliRunner().invoke(specs, ["list", "--root", str(root), "--name", "LOGIN url"])

    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert [record["coordinate"] for record in records] == ["authentication.fresh-code"]


def test_specs_list_filters_by_case_insensitive_step_text_substring(tmp_path: Path) -> None:
    root = write_spec_corpus(tmp_path / "specs", _VALID_CORPUS)

    result = CliRunner().invoke(specs, ["list", "--root", str(root), "--step", "ANOTHER data directory"])

    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert [record["coordinate"] for record in records] == ["authentication.foreign-token"]


def test_specs_list_combines_filters_with_and_semantics(tmp_path: Path) -> None:
    root = write_spec_corpus(tmp_path / "specs", _VALID_CORPUS)
    runner = CliRunner()

    both_match = runner.invoke(specs, ["list", "--root", str(root), "--tag", "fresh-code", "--step", "login url"])
    tag_matches_step_does_not = runner.invoke(
        specs, ["list", "--root", str(root), "--tag", "fresh-code", "--step", "data directory"]
    )

    assert both_match.exit_code == 0, both_match.output
    records = [json.loads(line) for line in both_match.stdout.splitlines()]
    assert [record["coordinate"] for record in records] == ["authentication.fresh-code"]
    assert tag_matches_step_does_not.exit_code == 0
    assert tag_matches_step_does_not.stdout == ""


# A corpus with nested subfolders and a root-level unit whose identity tag is
# an area name, to pin down --area's segment-granular, folder-only matching.
_AREA_CORPUS = {
    "invariants.feature": ("Feature: Corpus invariants\n\n  @authentication\n  Rule: a root-level rule\n"),
    "authentication/signin.feature": ("Feature: Sign-in\n\n  @fresh-code\n  Scenario: fresh\n    Given a thing\n"),
    "authentication/oauth/flow.feature": ("Feature: OAuth\n\n  @nested\n  Scenario: nested\n    Given a thing\n"),
    "networking/tunnels/hole-punching.feature": ("Feature: Tunnels\n\n  @deep\n  Scenario: deep\n    Given a thing\n"),
}


def test_specs_list_area_selects_folder_subtree_and_excludes_a_root_tag_of_the_same_name(tmp_path: Path) -> None:
    root = write_spec_corpus(tmp_path / "specs", _AREA_CORPUS)

    result = CliRunner().invoke(specs, ["list", "--root", str(root), "--area", "authentication"])

    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in result.stdout.splitlines()]
    # Units in authentication/ and its nested subfolder match; the root-level @authentication rule does not.
    assert [record["coordinate"] for record in records] == ["authentication.oauth.nested", "authentication.fresh-code"]


def test_specs_list_area_matches_a_deeper_subfolder_by_full_segments(tmp_path: Path) -> None:
    root = write_spec_corpus(tmp_path / "specs", _AREA_CORPUS)

    result = CliRunner().invoke(specs, ["list", "--root", str(root), "--area", "authentication.oauth"])

    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert [record["coordinate"] for record in records] == ["authentication.oauth.nested"]


def test_specs_list_area_is_segment_granular_not_a_string_prefix(tmp_path: Path) -> None:
    root = write_spec_corpus(tmp_path / "specs", _AREA_CORPUS)

    result = CliRunner().invoke(specs, ["list", "--root", str(root), "--area", "auth"])

    # 'auth' is a prefix of the string 'authentication' but not a whole folder segment, so nothing matches.
    assert result.exit_code == 0
    assert result.stdout == ""
