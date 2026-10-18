from pathlib import Path

from imbue.mngr.utils.agent_skills import code_regions
from imbue.mngr.utils.agent_skills import mngr_subcommands_referenced
from imbue.mngr.utils.agent_skills import skill_shape_problems
from imbue.mngr.utils.agent_skills import split_skill_frontmatter


def test_code_regions_keeps_fenced_blocks_and_inline_spans_and_drops_prose() -> None:
    markdown = "Run `mngr list` first.\n\n```bash\nmngr create x\n```\n\nThen mngr destroy is prose.\n"
    regions = code_regions(markdown)
    assert "mngr create x\n" in regions
    assert "mngr list" in regions
    assert not any("destroy" in region for region in regions)


def test_mngr_subcommands_referenced_ignores_branch_names_env_vars_and_paths() -> None:
    markdown = "`mngr/some-branch` `$MNGR_AGENT_ID` `mngr --help` `/usr/bin/mngr status` `mngr create` `mngr imbue_cloud auth`"
    assert mngr_subcommands_referenced(markdown) == {"create", "imbue_cloud"}


def test_split_skill_frontmatter_reads_lenient_key_values_and_tolerates_no_block() -> None:
    document = split_skill_frontmatter(
        '---\nname: find-agent\ndescription: "Find it"\nargument-hint: [a] [b...]\n---\nbody line\n'
    )
    assert document.fields == {"name": "find-agent", "description": "Find it", "argument-hint": "[a] [b...]"}
    assert document.body == "body line"
    assert split_skill_frontmatter("no frontmatter here").fields == {}


def test_skill_shape_problems_names_each_violation(tmp_path: Path) -> None:
    skill_dir = tmp_path / "good-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("---\nname: good-skill\ndescription: Does a thing.\n---\nbody\n")
    assert skill_shape_problems(skill_file) == ()

    skill_file.write_text("---\nname: other\n---\n" + "line\n" * 501)
    problems = skill_shape_problems(skill_file)
    assert any("directory name" in problem for problem in problems)
    assert any("description" in problem for problem in problems)
    assert any("501 lines" in problem for problem in problems)
