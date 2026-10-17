import re
import subprocess
import textwrap
from pathlib import Path

import pytest

from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_BARE_TMUX_TARGETS
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_TRAILING_COMMENTS
from imbue.imbue_common.ratchet_testing.common_ratchets import check_ratchet_rule

_BARE_TMUX_TARGET_PATTERN = re.compile(PREVENT_BARE_TMUX_TARGETS.pattern_string, re.MULTILINE)


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


@pytest.mark.parametrize(
    "line",
    [
        'while tmux has-session -t "$SESSION_NAME" 2>/dev/null; do',
        "tmux send-keys -t 'mngr-agent' Enter",
        'tmux kill-session -t "${NAME}"',
        'tmux has-session -t "$1"',
    ],
)
def test_bare_tmux_target_rule_flags_name_targets_without_exact_match(line: str) -> None:
    assert _BARE_TMUX_TARGET_PATTERN.search(line) is not None


@pytest.mark.parametrize(
    "line",
    [
        'while tmux has-session -t "=$SESSION_NAME" 2>/dev/null; do',
        'while [ -n "$SESSION_ID" ] && tmux has-session -t "$SESSION_ID" 2>/dev/null; do',
        "SESSION_ID=$(tmux display-message -p -t \"$TMUX_PANE\" '#{session_id}')",
        'tmux display-message -p -t "${TMUX_PANE}" x',
        "tmux has-session -t '$3'",
        "tmux capture-pane -p -t '%12'",
        '# tmux has-session -t "$SESSION"',
    ],
)
def test_bare_tmux_target_rule_accepts_exact_names_and_tmux_ids(line: str) -> None:
    assert _BARE_TMUX_TARGET_PATTERN.search(line) is None
