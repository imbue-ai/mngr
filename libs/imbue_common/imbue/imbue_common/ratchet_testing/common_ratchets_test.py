import subprocess
import textwrap
from pathlib import Path

from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_TRAILING_COMMENTS
from imbue.imbue_common.ratchet_testing.common_ratchets import check_ratchet_rule


def _commit_module(git_repo: Path, source: str) -> None:
    (git_repo / "module.py").write_text(textwrap.dedent(source).lstrip("\n"))
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add module"], cwd=git_repo, check=True, capture_output=True)


def test_trailing_comment_rule_finds_a_comment_after_code(git_repo: Path) -> None:
    _commit_module(
        git_repo,
        """
        def f() -> int:
            value = 1  # the answer
            return value
        """,
    )

    chunks = check_ratchet_rule(PREVENT_TRAILING_COMMENTS, git_repo)

    assert len(chunks) == 1
    assert chunks[0].start_line == 2


def test_trailing_comment_rule_ignores_a_hex_colour_closing_a_string_literal(git_repo: Path) -> None:
    _commit_module(
        git_repo,
        """
        def f(body: str) -> None:
            assert "background: #e9ecd9" in body
            assert 'color: #FFF' in body
        """,
    )

    assert check_ratchet_rule(PREVENT_TRAILING_COMMENTS, git_repo) == ()
