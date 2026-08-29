import subprocess
import textwrap
from pathlib import Path

from imbue.imbue_common.ratchet_testing.ratchets import find_if_elif_without_else


def _commit_module(git_repo: Path, source: str) -> None:
    (git_repo / "module.py").write_text(textwrap.dedent(source).lstrip("\n"))
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add module"], cwd=git_repo, check=True, capture_output=True)


def test_finds_elif_chain_without_else(git_repo: Path) -> None:
    _commit_module(
        git_repo,
        """
        def f(x: int) -> str:
            if x == 1:
                return "one"
            elif x == 2:
                return "two"
            return "other"
        """,
    )

    chunks = find_if_elif_without_else(git_repo)

    assert len(chunks) == 1
    assert chunks[0].start_line == 2
    assert chunks[0].end_line == 5


def test_ignores_elif_chain_with_else(git_repo: Path) -> None:
    _commit_module(
        git_repo,
        """
        def f(x: int) -> str:
            if x == 1:
                return "one"
            elif x == 2:
                return "two"
            else:
                return "other"
        """,
    )

    assert find_if_elif_without_else(git_repo) == ()


def test_ignores_else_block_that_begins_with_an_if(git_repo: Path) -> None:
    # The else body is more than the guard, so this is a real else clause and not
    # an elif -- the two are only indistinguishable when the body is the if alone.
    _commit_module(
        git_repo,
        """
        def f(x: int, y: int) -> str:
            if x == 1:
                return "one"
            else:
                if y == 0:
                    return "guarded"
                return "other"
        """,
    )

    assert find_if_elif_without_else(git_repo) == ()


def test_finds_else_block_holding_only_an_if(git_repo: Path) -> None:
    # Written this way, the else is indistinguishable from an elif in the AST, so it
    # is read as one: the chain leaves the case where neither condition holds unhandled.
    _commit_module(
        git_repo,
        """
        def f(x: int, y: int) -> str | None:
            if x == 1:
                return "one"
            else:
                if y == 0:
                    return "zero"
            return None
        """,
    )

    assert len(find_if_elif_without_else(git_repo)) == 1
