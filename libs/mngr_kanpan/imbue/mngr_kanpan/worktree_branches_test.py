import os
import shutil
from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_git_command
from imbue.mngr_kanpan.worktree_branches import MAX_BRANCHES_PER_AGENT
from imbue.mngr_kanpan.worktree_branches import WorktreeBranches
from imbue.mngr_kanpan.worktree_branches import parse_reflog_checkout_targets
from imbue.mngr_kanpan.worktree_branches import read_worktree_branches
from imbue.mngr_kanpan.worktree_branches import select_agent_branches

_REFLOG_PREFIX = "0" * 40 + " " + "1" * 40 + " Test User <test@example.com> 1700000000 -0700\t"
# A reflog entry whose committer name is Latin-1 rather than UTF-8, as git writes it verbatim.
_LATIN_1_REFLOG_LINE = b"0" * 40 + b" " + b"1" * 40 + b" Jos\xe9 <jose@example.com> 1700000000 -0700\tcommit: work\n"


def _log_git_invocations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a `git` on PATH that records one line per invocation, and return that log.

    The stand-in forwards to the real git, so the commands under test still do
    real work; only how often each ran is observed. The arguments are written
    with a single `printf`, because these run concurrently and two writes would
    interleave into one another's lines.
    """
    real_git = shutil.which("git")
    assert real_git is not None
    bin_dir = tmp_path / "git_shim_bin"
    bin_dir.mkdir()
    git_argv_log = tmp_path / "git_argv.log"
    shim = bin_dir / "git"
    shim.write_text(
        "#!/bin/sh\n" + 'printf "%s\\n" "$*" >> "' + str(git_argv_log) + '"\n' + "exec " + real_git + ' "$@"\n'
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    git_argv_log.write_text("")
    return git_argv_log


def _reflog(*messages: str) -> str:
    """Build a git `logs/HEAD` body carrying the given reflog messages, oldest first."""
    return "".join(f"{_REFLOG_PREFIX}{message}\n" for message in messages)


# === parse_reflog_checkout_targets ===


def test_parse_reflog_checkout_targets_returns_targets_most_recent_first() -> None:
    reflog = _reflog(
        "checkout: moving from main to feature/one",
        "checkout: moving from feature/one to feature/two",
    )
    assert parse_reflog_checkout_targets(reflog) == ("feature/two", "feature/one")


def test_parse_reflog_checkout_targets_ignores_non_checkout_messages() -> None:
    reflog = _reflog(
        "commit (initial): init",
        "checkout: moving from main to feature/one",
        "commit: work",
        "reset: moving to HEAD",
        "merge origin/main: Fast-forward",
    )
    assert parse_reflog_checkout_targets(reflog) == ("feature/one",)


def test_parse_reflog_checkout_targets_on_empty_reflog() -> None:
    assert parse_reflog_checkout_targets("") == ()


# === select_agent_branches ===


def test_select_agent_branches_keeps_caller_priority_order() -> None:
    selected = select_agent_branches(["feature/current", "feature/recorded", "feature/older"], MAX_BRANCHES_PER_AGENT)
    assert selected == ("feature/current", "feature/recorded", "feature/older")


def test_select_agent_branches_skips_absent_refs() -> None:
    selected = select_agent_branches([None, "feature/one", None], MAX_BRANCHES_PER_AGENT)
    assert selected == ("feature/one",)


def test_select_agent_branches_deduplicates_keeping_first_position() -> None:
    selected = select_agent_branches(
        ["feature/one", "feature/two", "feature/one", "feature/two"], MAX_BRANCHES_PER_AGENT
    )
    assert selected == ("feature/one", "feature/two")


def test_select_agent_branches_drops_refs_no_agent_opens_a_pr_from() -> None:
    """Trunk branches and the `HEAD` a detached checkout reports are refs an agent
    never opens a PR from.
    """
    selected = select_agent_branches(["HEAD", "main", "master", "feature/real"], MAX_BRANCHES_PER_AGENT)
    assert selected == ("feature/real",)


def test_select_agent_branches_truncates_to_max_branch_count() -> None:
    selected = select_agent_branches([f"feature/{index}" for index in range(20)], 3)
    assert selected == ("feature/0", "feature/1", "feature/2")


def test_select_agent_branches_selects_nothing_for_a_zero_count() -> None:
    assert select_agent_branches(["feature/one", "feature/two"], 0) == ()


# === read_worktree_branches ===


def test_read_worktree_branches_reports_every_branch_the_worktree_hosted(
    temp_git_repo: Path, test_cg: ConcurrencyGroup
) -> None:
    """A worktree that moved from one branch onto a follow-up branch reports the
    follow-up as current and the earlier one in its history, so a board keyed on a
    single branch stops missing the other.
    """
    run_git_command(temp_git_repo, "checkout", "-b", "mngr/first-change")
    run_git_command(temp_git_repo, "checkout", "-b", "mngr/follow-up")

    branches_by_work_dir = read_worktree_branches([temp_git_repo], test_cg)

    assert branches_by_work_dir == {
        temp_git_repo: WorktreeBranches(
            current_branch="mngr/follow-up",
            previously_checked_out_branches=("mngr/follow-up", "mngr/first-change"),
        )
    }


def test_read_worktree_branches_orders_history_by_most_recent_checkout(
    temp_git_repo: Path, test_cg: ConcurrencyGroup
) -> None:
    run_git_command(temp_git_repo, "checkout", "-b", "mngr/oldest")
    run_git_command(temp_git_repo, "checkout", "-b", "mngr/middle")
    run_git_command(temp_git_repo, "checkout", "-b", "mngr/newest")
    run_git_command(temp_git_repo, "checkout", "mngr/middle")

    worktree = read_worktree_branches([temp_git_repo], test_cg)[temp_git_repo]

    assert worktree.current_branch == "mngr/middle"
    assert worktree.previously_checked_out_branches == ("mngr/middle", "mngr/newest", "mngr/middle", "mngr/oldest")


def test_read_worktree_branches_keeps_only_targets_that_are_local_branches(
    temp_git_repo: Path, test_cg: ConcurrencyGroup
) -> None:
    """Git records a checkout target the way the user spelled it, so a tag, an
    abbreviated commit, and a revision expression all land in the reflog looking like
    branch names. Each one kept would consume a branch slot an actual branch needs.
    """
    run_git_command(temp_git_repo, "checkout", "-b", "mngr/first-change")
    run_git_command(temp_git_repo, "tag", "v1")
    short_commit = run_git_command(temp_git_repo, "rev-parse", "--short", "HEAD").stdout.strip()
    run_git_command(temp_git_repo, "checkout", short_commit)
    run_git_command(temp_git_repo, "checkout", "v1")
    run_git_command(temp_git_repo, "checkout", "mngr/first-change")
    run_git_command(temp_git_repo, "checkout", "-b", "mngr/follow-up")

    worktree = read_worktree_branches([temp_git_repo], test_cg)[temp_git_repo]

    assert set(worktree.previously_checked_out_branches) == {"mngr/follow-up", "mngr/first-change"}
    assert select_agent_branches(
        [worktree.current_branch, *worktree.previously_checked_out_branches], MAX_BRANCHES_PER_AGENT
    ) == ("mngr/follow-up", "mngr/first-change")


def test_read_worktree_branches_drops_a_branch_that_no_longer_exists(
    temp_git_repo: Path, test_cg: ConcurrencyGroup
) -> None:
    run_git_command(temp_git_repo, "checkout", "-b", "mngr/deleted-later")
    run_git_command(temp_git_repo, "checkout", "-b", "mngr/kept")
    run_git_command(temp_git_repo, "branch", "-D", "mngr/deleted-later")

    worktree = read_worktree_branches([temp_git_repo], test_cg)[temp_git_repo]

    assert worktree.previously_checked_out_branches == ("mngr/kept",)


def test_read_worktree_branches_reports_a_detached_head_verbatim(
    temp_git_repo: Path, test_cg: ConcurrencyGroup
) -> None:
    """Filtering is `select_agent_branches`'s job, so a detached checkout is reported
    as git spells it rather than being dropped here.
    """
    run_git_command(temp_git_repo, "checkout", "--detach", "HEAD")

    assert read_worktree_branches([temp_git_repo], test_cg)[temp_git_repo].current_branch == "HEAD"


def test_read_worktree_branches_reads_a_linked_worktree(
    temp_git_repo: Path, tmp_path: Path, test_cg: ConcurrencyGroup
) -> None:
    """A linked worktree -- how mngr gives each agent its own checkout -- keeps its
    git metadata under the main repo's `.git/worktrees/<name>`, so its reflog is only
    found by resolving the git dir rather than assuming `<work_dir>/.git`. Nothing
    ever moves *to* the branch such a worktree is created on, so that branch is
    absent from its own history.
    """
    linked_work_dir = tmp_path / "linked"
    run_git_command(temp_git_repo, "worktree", "add", "-b", "mngr/linked-first", str(linked_work_dir))
    run_git_command(linked_work_dir, "checkout", "-b", "mngr/linked-follow-up")

    worktree = read_worktree_branches([linked_work_dir], test_cg)[linked_work_dir]

    assert worktree.current_branch == "mngr/linked-follow-up"
    assert worktree.previously_checked_out_branches == ("mngr/linked-follow-up",)


def test_read_worktree_branches_covers_several_work_dirs(
    temp_git_repo: Path, tmp_path: Path, test_cg: ConcurrencyGroup
) -> None:
    other_work_dir = tmp_path / "other"
    run_git_command(temp_git_repo, "worktree", "add", "-b", "mngr/other-work", str(other_work_dir))
    run_git_command(temp_git_repo, "checkout", "-b", "mngr/main-work")

    branches_by_work_dir = read_worktree_branches([temp_git_repo, other_work_dir], test_cg)

    assert branches_by_work_dir[temp_git_repo].current_branch == "mngr/main-work"
    assert branches_by_work_dir[other_work_dir].current_branch == "mngr/other-work"


def test_read_worktree_branches_reads_a_reflog_that_is_not_valid_utf8(
    temp_git_repo: Path, test_cg: ConcurrencyGroup
) -> None:
    """Git writes committer identities and branch names to the reflog as raw bytes, and
    the process reading them need not be running under a UTF-8 locale. Neither may cost
    the board its branch history, let alone every GitHub column on it.
    """
    run_git_command(temp_git_repo, "checkout", "-b", "mngr/first-change")
    run_git_command(temp_git_repo, "checkout", "-b", "mngr/follow-up")
    reflog_path = temp_git_repo / ".git" / "logs" / "HEAD"
    reflog_path.write_bytes(reflog_path.read_bytes() + _LATIN_1_REFLOG_LINE)

    worktree = read_worktree_branches([temp_git_repo], test_cg)[temp_git_repo]

    assert worktree.previously_checked_out_branches == ("mngr/follow-up", "mngr/first-change")


def test_read_worktree_branches_omits_a_directory_that_is_not_a_repo(
    tmp_path: Path, test_cg: ConcurrencyGroup
) -> None:
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()

    assert read_worktree_branches([not_a_repo], test_cg) == {}


def test_read_worktree_branches_on_no_work_dirs(test_cg: ConcurrencyGroup) -> None:
    assert read_worktree_branches([], test_cg) == {}


def test_read_worktree_branches_lists_a_repository_s_branches_once_for_all_its_worktrees(
    temp_git_repo: Path, tmp_path: Path, test_cg: ConcurrencyGroup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every worktree of a repository shares one branch list, so a board holding many
    agents on one repository asks git for it once rather than once per agent.
    """
    work_dirs = [temp_git_repo]
    for index in range(3):
        linked_work_dir = tmp_path / f"linked-{index}"
        run_git_command(temp_git_repo, "worktree", "add", "-b", f"mngr/work-{index}", str(linked_work_dir))
        work_dirs.append(linked_work_dir)
    git_argv_log = _log_git_invocations(tmp_path, monkeypatch)

    branches_by_work_dir = read_worktree_branches(work_dirs, test_cg)

    assert len(branches_by_work_dir) == len(work_dirs)
    invocations = git_argv_log.read_text().splitlines()
    assert len([line for line in invocations if "rev-parse" in line]) == len(work_dirs)
    assert len([line for line in invocations if "for-each-ref" in line]) == 1


def test_read_worktree_branches_lists_branches_once_per_repository(
    temp_git_repo: Path, tmp_path: Path, test_cg: ConcurrencyGroup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two separate repositories are two branch lists, so neither borrows the other's."""
    other_repo = tmp_path / "other_repo"
    init_git_repo(other_repo)
    run_git_command(temp_git_repo, "checkout", "-b", "mngr/first-repo-work")
    run_git_command(other_repo, "checkout", "-b", "mngr/second-repo-work")
    git_argv_log = _log_git_invocations(tmp_path, monkeypatch)

    branches_by_work_dir = read_worktree_branches([temp_git_repo, other_repo], test_cg)

    assert branches_by_work_dir[temp_git_repo].current_branch == "mngr/first-repo-work"
    assert branches_by_work_dir[other_repo].current_branch == "mngr/second-repo-work"
    invocations = git_argv_log.read_text().splitlines()
    assert len([line for line in invocations if "for-each-ref" in line]) == 2
