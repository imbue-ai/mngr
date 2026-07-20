"""Tests for the ``minds specs`` CLI group.

Every corpus is synthetic, built under ``tmp_path`` via ``write_spec_corpus``
and pointed at with ``--root``; nothing here touches the live
``apps/minds/specs/`` corpus.
"""

import json
from pathlib import Path

from click.testing import CliRunner

from imbue.minds.cli.specs import specs
from imbue.minds.cli_entry import cli
from imbue.minds.core.behavioral_specs.testing import write_spec_corpus

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


def test_specs_query_filters_by_exact_tag_or_coordinate(tmp_path: Path) -> None:
    root = write_spec_corpus(tmp_path / "specs", _VALID_CORPUS)
    runner = CliRunner()

    by_raw_tag = runner.invoke(specs, ["query", "--root", str(root), "--tag", "fresh-code"])
    by_coordinate = runner.invoke(specs, ["query", "--root", str(root), "--tag", "authentication.fresh-code"])
    with_sigil = runner.invoke(specs, ["query", "--root", str(root), "--tag", "@fresh-code"])
    no_match = runner.invoke(specs, ["query", "--root", str(root), "--tag", "fresh"])

    for result in (by_raw_tag, by_coordinate, with_sigil):
        assert result.exit_code == 0, result.output
        records = [json.loads(line) for line in result.stdout.splitlines()]
        assert [record["coordinate"] for record in records] == ["authentication.fresh-code"]
    assert no_match.exit_code == 0
    assert no_match.stdout == ""


def test_specs_query_filters_by_case_insensitive_name_substring(tmp_path: Path) -> None:
    root = write_spec_corpus(tmp_path / "specs", _VALID_CORPUS)

    result = CliRunner().invoke(specs, ["query", "--root", str(root), "--name", "LOGIN url"])

    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert [record["coordinate"] for record in records] == ["authentication.fresh-code"]


def test_specs_query_filters_by_case_insensitive_step_text_substring(tmp_path: Path) -> None:
    root = write_spec_corpus(tmp_path / "specs", _VALID_CORPUS)

    result = CliRunner().invoke(specs, ["query", "--root", str(root), "--step", "ANOTHER data directory"])

    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert [record["coordinate"] for record in records] == ["authentication.foreign-token"]


def test_specs_query_combines_filters_with_and_semantics(tmp_path: Path) -> None:
    root = write_spec_corpus(tmp_path / "specs", _VALID_CORPUS)
    runner = CliRunner()

    both_match = runner.invoke(specs, ["query", "--root", str(root), "--tag", "fresh-code", "--step", "login url"])
    tag_matches_step_does_not = runner.invoke(
        specs, ["query", "--root", str(root), "--tag", "fresh-code", "--step", "data directory"]
    )

    assert both_match.exit_code == 0, both_match.output
    records = [json.loads(line) for line in both_match.stdout.splitlines()]
    assert [record["coordinate"] for record in records] == ["authentication.fresh-code"]
    assert tag_matches_step_does_not.exit_code == 0
    assert tag_matches_step_does_not.stdout == ""


def test_specs_group_is_registered_on_the_minds_cli() -> None:
    result = CliRunner().invoke(cli, ["specs", "--help"])

    assert result.exit_code == 0, result.output
    assert "validate" in result.output
    assert "query" in result.output
