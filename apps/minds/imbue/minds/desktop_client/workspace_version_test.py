import subprocess
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import pytest
from pydantic import Field
from pydantic import PrivateAttr

from imbue.minds.desktop_client.backup_workspace_scripts import OFFICIAL_REMOTE_URL
from imbue.minds.desktop_client.testing import exec_json_envelope
from imbue.minds.desktop_client.workspace_version import _GIT_DESCRIBE_ARGS
from imbue.minds.desktop_client.workspace_version import _GIT_UPDATE_SELF_ARGS
from imbue.minds.desktop_client.workspace_version import parse_git_describe
from imbue.minds.desktop_client.workspace_version import parse_update_self_ref
from imbue.minds.desktop_client.workspace_version import parse_upgrade_merges
from imbue.minds.desktop_client.workspace_version import read_workspace_current_version
from imbue.minds.desktop_client.workspace_version import read_workspace_git_version
from imbue.minds.testing import run_git_for_backup_test
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId

# The release the published template was cut from: tagged in the official
# repo, reachable from the workspace's HEAD, never present in its clone.
_BASE_TAG: Final[str] = "minds-v0.4.1"


class _GitAnsweringCaller(MngrCaller):
    """Answers the one version-read exec with canned stdout, recording the shell command it was asked to run."""

    stdout: str = Field(default="")
    _git_commands: list[str] = PrivateAttr(default_factory=list)

    def call(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
        env_overrides: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> MngrCallResult:
        self._git_commands.append(argv[3])
        return MngrCallResult(returncode=0, stdout=exec_json_envelope(self.stdout))

    @property
    def git_commands(self) -> list[str]:
        return self._git_commands


def test_parse_git_describe_returns_tag() -> None:
    assert parse_git_describe("minds-v0.3.3\n") == "minds-v0.3.3"


def test_parse_git_describe_returns_none_when_empty() -> None:
    assert parse_git_describe("") is None
    assert parse_git_describe("   \n") is None


def test_parse_update_self_ref_names_the_ref_the_run_moved_to() -> None:
    assert parse_update_self_ref("update-self: merge upstream template (minds-v0.4.1)\n") == "minds-v0.4.1"


def test_parse_update_self_ref_reports_a_branch_target_as_written() -> None:
    assert parse_update_self_ref("update-self: merge upstream template (main)") == "main"


def test_parse_update_self_ref_ignores_the_templates_own_update_self_commits() -> None:
    """Upstream commits that change the skill share the prefix and would shadow the real marker."""
    assert (
        parse_update_self_ref("update-self: survive cross-version launches -- restore the lead_agent, fail fast")
        is None
    )
    assert parse_update_self_ref("Revert the update-self run (it broke the terminal)") is None
    assert parse_update_self_ref("") is None
    assert parse_update_self_ref("update-self: merge upstream template") is None


@pytest.mark.parametrize(
    ("marker_ref", "tag", "expected"),
    [
        pytest.param("minds-v0.4.1", "minds-v0.3.17", "minds-v0.4.1", id="marker-outranks-an-older-release-tag"),
        pytest.param("minds-v0.6.2", "minds-v0.7.0", "minds-v0.7.0", id="a-newer-release-tag-outranks-the-marker"),
        pytest.param(
            "minds-v0.7.0-rc.1", "minds-v0.7.0", "minds-v0.7.0", id="a-release-outranks-its-prerelease-marker"
        ),
        # A workspace updated to ``main`` still describes an older release; only two releases compare.
        pytest.param("main", "minds-v0.7.0", "main", id="a-branch-marker-outranks-any-tag"),
    ],
)
def test_the_marker_and_the_tag_rank_by_release(marker_ref: str, tag: str, expected: str) -> None:
    caller = _GitAnsweringCaller(stdout=f"update-self: merge upstream template ({marker_ref})\n{tag}\n")

    assert read_workspace_current_version(agent_address=AgentId.generate(), mngr_caller=caller) == expected


def test_the_marker_and_the_tag_are_read_in_one_exec() -> None:
    """Both git reads ride one ``mngr exec``."""
    caller = _GitAnsweringCaller(stdout="minds-v0.4.1\n")

    read_workspace_current_version(agent_address=AgentId.generate(), mngr_caller=caller)

    assert len(caller.git_commands) == 1
    (command,) = caller.git_commands
    assert "--grep" in command
    assert "describe" in command


def test_a_workspace_with_no_marker_falls_back_to_the_tag() -> None:
    caller = _GitAnsweringCaller(stdout="minds-v0.4.1\n")

    version = read_workspace_current_version(agent_address=AgentId.generate(), mngr_caller=caller)

    assert version == "minds-v0.4.1"


def test_a_tagless_workspace_is_read_from_its_marker() -> None:
    """The create path checks the release out as a branch, so a fresh clone has no tags to describe."""
    caller = _GitAnsweringCaller(stdout="update-self: merge upstream template (minds-v0.4.1)\n")

    assert read_workspace_current_version(agent_address=AgentId.generate(), mngr_caller=caller) == "minds-v0.4.1"


def test_a_marker_the_strict_parse_rejects_does_not_shadow_the_tag() -> None:
    """The grep selects any subject that starts with the marker prefix; only an exact marker names a version."""
    caller = _GitAnsweringCaller(stdout="update-self: merge upstream template (minds-v0.4.1) [retry]\nminds-v0.3.17\n")

    assert read_workspace_current_version(agent_address=AgentId.generate(), mngr_caller=caller) == "minds-v0.3.17"


def test_a_workspace_with_neither_marker_nor_tag_has_no_version() -> None:
    assert read_workspace_current_version(agent_address=AgentId.generate(), mngr_caller=_GitAnsweringCaller()) is None


def test_parse_upgrade_merges_parses_tab_separated_lines() -> None:
    stdout = (
        "aaaa1111\t2026-06-01T12:00:00+00:00\tupgrade attempt 2: minds-v0.3.2 -> minds-v0.3.3\n"
        "bbbb2222\t2026-05-01T09:30:00+00:00\tupgrade attempt 1: minds-v0.3.1 -> minds-v0.3.2\n"
    )

    merges = parse_upgrade_merges(stdout)

    assert len(merges) == 2
    assert merges[0].commit_sha == "aaaa1111"
    assert merges[0].summary == "upgrade attempt 2: minds-v0.3.2 -> minds-v0.3.3"
    assert merges[0].committed_at is not None
    assert merges[0].committed_at.tzinfo is not None
    assert merges[1].commit_sha == "bbbb2222"


def test_parse_upgrade_merges_tolerates_empty_subject_and_unparseable_time() -> None:
    stdout = "cccc3333\tnot-a-time\t\n"

    merges = parse_upgrade_merges(stdout)

    assert len(merges) == 1
    assert merges[0].commit_sha == "cccc3333"
    assert merges[0].summary == ""
    assert merges[0].committed_at is None


def test_parse_upgrade_merges_skips_blank_and_malformed_lines() -> None:
    stdout = "\n  \nonlyonefield\ndddd4444\t2026-06-01T12:00:00Z\tmerged\n"

    merges = parse_upgrade_merges(stdout)

    assert len(merges) == 1
    assert merges[0].commit_sha == "dddd4444"


def test_parse_upgrade_merges_handles_tabs_in_subject() -> None:
    # The subject is the third field; an embedded tab in the message must not
    # split it (split has maxsplit=2).
    stdout = "eeee5555\t2026-06-01T12:00:00Z\tmerged\twith\ttabs\n"

    merges = parse_upgrade_merges(stdout)

    assert len(merges) == 1
    assert merges[0].summary == "merged\twith\ttabs"


def test_parse_upgrade_merges_empty_output_is_empty_tuple() -> None:
    assert parse_upgrade_merges("") == ()


def test_version_read_exec_never_starts_a_stopped_host() -> None:
    """The version read is best-effort diagnostics; its execs must pass --no-start.

    ``mngr exec`` auto-starts a stopped host by default, so without the flag a
    mere version read of an offline machine cold-boots its container as a side
    effect (observed live: a background exec silently started a container the
    recovery flow believed was stopped). The git command must also be a single
    COMMAND token: ``mngr exec`` parses extra positional tokens as agent names
    (there is no ``-- ARGS...`` form), so a multi-token git command errors out
    before ever reaching the machine.
    """
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1))
    agent_id = AgentId.generate()
    read_workspace_git_version(agent_address=agent_id, mngr_caller=caller)
    assert len(caller.calls) == 2
    for argv in caller.calls:
        assert argv[0] == "exec"
        assert "--no-start" in argv
        assert "--" not in argv
        assert str(agent_id) in argv
        git_commands = [token for token in argv if "git " in token]
        assert len(git_commands) == 1


def test_version_read_parses_the_json_exec_envelope() -> None:
    """A successful exec's stdout is a ``--format json`` envelope; the command's
    own stdout must be extracted from it (raw human-format stdout would carry
    mngr's trailing ``Command succeeded on agent <name>`` status line).
    """
    envelope = exec_json_envelope("minds-v1.2.3\n")
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=envelope))
    version = read_workspace_git_version(agent_address=AgentId.generate(), mngr_caller=caller)
    assert version.current_minds_version == "minds-v1.2.3"


class _LocalGitCaller(MngrCaller):
    """Runs the version read's git command for real, in a local repo, the way a workspace's shell would.

    The command reaches ``sh`` -- the shell every host runs an ``mngr exec``
    command under, and a POSIX one rather than bash in a workspace container --
    verbatim except for the official template URL, which is repointed at a local
    repo so the tag fetch never leaves the machine. The shell's exit code and
    stderr are recorded here because the read returns neither -- it answers with
    the parsed stdout alone, having spent the exit code on its own failure check
    and the stderr on a debug line.
    """

    repo: Path = Field(description="Repo the command runs in, standing in for the workspace's checkout")
    official_url: str = Field(description="What the official template URL is rewritten to")
    _shell_returncodes: list[int] = PrivateAttr(default_factory=list)
    _shell_stderrs: list[str] = PrivateAttr(default_factory=list)

    def call(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
        env_overrides: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> MngrCallResult:
        command = argv[3]
        assert OFFICIAL_REMOTE_URL in command, f"nothing to repoint at a local repo in: {command}"
        result = subprocess.run(
            ["sh", "-c", command.replace(OFFICIAL_REMOTE_URL, self.official_url)],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        self._shell_returncodes.append(result.returncode)
        self._shell_stderrs.append(result.stderr)
        return MngrCallResult(
            returncode=result.returncode,
            stdout=exec_json_envelope(result.stdout, stderr=result.stderr),
        )

    @property
    def shell_returncodes(self) -> list[int]:
        return self._shell_returncodes

    @property
    def shell_stderrs(self) -> list[str]:
        return self._shell_stderrs


def _make_published_template_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """Build the git shape publishing a template produces; return (workspace clone, official repo).

    Mirrors publish-template: the base commit is tagged in the official
    template repo, the published repo receives only the snapshot commit on top
    of it, pushed as ``<sha>:refs/heads/main`` with no tags, and the workspace
    is a clone of that -- the base's whole history, none of its tags. The tree
    carries ``system/config/parent.toml``, which is what marks it as descended
    from the template (a published template keeps it).
    """
    official = tmp_path / "official.git"
    published = tmp_path / "published.git"
    for bare in (official, published):
        # -b main: the clone below checks out whatever the published repo's HEAD
        # names, and an inherited ``init.defaultBranch`` names a branch the push
        # never creates.
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True, capture_output=True, timeout=60
        )
    template = tmp_path / "template"
    template.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(template)], check=True, capture_output=True, timeout=60)
    (template / "file.txt").write_text("base\n")
    parent_toml = template / "system" / "config" / "parent.toml"
    parent_toml.parent.mkdir(parents=True)
    parent_toml.write_text('url = "https://github.com/imbue-ai/default-workspace-template.git"\nbranch = "main"\n')
    run_git_for_backup_test(template, "add", "-A")
    run_git_for_backup_test(template, "commit", "-q", "-m", "base commit")
    run_git_for_backup_test(template, "tag", _BASE_TAG)
    run_git_for_backup_test(template, "push", "-q", str(official), "main", "--tags")
    (template / "file.txt").write_text("snapshot\n")
    run_git_for_backup_test(template, "add", "-A")
    run_git_for_backup_test(template, "commit", "-q", "-m", "Initial workspace commit")
    snapshot_sha = run_git_for_backup_test(template, "rev-parse", "HEAD").strip()
    run_git_for_backup_test(template, "push", "-q", str(published), f"{snapshot_sha}:refs/heads/main")
    workspace = tmp_path / "workspace"
    subprocess.run(["git", "clone", "-q", str(published), str(workspace)], check=True, capture_output=True, timeout=60)
    return workspace, official


def _local_minds_tags(repo: Path) -> list[str]:
    return run_git_for_backup_test(repo, "tag", "-l", "minds-v*").split()


def test_a_published_template_clone_has_nothing_to_read_before_the_fetch(tmp_path: Path) -> None:
    """Control: the marker log and the describe -- all the command was before the fetch -- both answer nothing.

    This is the "Version unknown" a workspace created from a published template
    is stuck at without the fetch.
    """
    workspace, _official = _make_published_template_workspace(tmp_path)

    marker = subprocess.run(
        _GIT_UPDATE_SELF_ARGS, cwd=workspace, capture_output=True, text=True, check=True, timeout=60
    )
    describe = subprocess.run(
        _GIT_DESCRIBE_ARGS, cwd=workspace, capture_output=True, text=True, check=False, timeout=60
    )

    assert marker.stdout.strip() == ""
    assert describe.returncode != 0
    assert describe.stdout.strip() == ""


@pytest.mark.witnesses("workspace-updates.version-recovered-from-the-template", partial="the version read only")
def test_a_published_template_clone_reads_its_base_tag(tmp_path: Path) -> None:
    """The whole point: one version read fetches the release tags and the clone describes as its base."""
    workspace, official = _make_published_template_workspace(tmp_path)
    caller = _LocalGitCaller(repo=workspace, official_url=str(official))

    version = read_workspace_current_version(agent_address=AgentId.generate(), mngr_caller=caller)

    assert version == _BASE_TAG
    assert _local_minds_tags(workspace) == [_BASE_TAG]


def test_the_tag_fetch_repoints_an_official_remote_left_pointing_elsewhere(tmp_path: Path) -> None:
    """minds owns the ``official`` remote name here as it does in the backup scripts: a stale one is repointed."""
    workspace, official = _make_published_template_workspace(tmp_path)
    run_git_for_backup_test(workspace, "remote", "add", "official", str(tmp_path / "somewhere-else"))
    caller = _LocalGitCaller(repo=workspace, official_url=str(official))

    version = read_workspace_current_version(agent_address=AgentId.generate(), mngr_caller=caller)

    assert version == _BASE_TAG
    assert run_git_for_backup_test(workspace, "remote", "get-url", "official").strip() == str(official)


def test_the_tag_fetch_stops_once_the_tag_is_local(tmp_path: Path) -> None:
    """A second read answers from the clone: the sweep pays for the fetch only while there is no version.

    ``FETCH_HEAD`` is the evidence -- every fetch writes it, so removing it
    after the first read and finding it still gone after the second means no
    second fetch was made.
    """
    workspace, official = _make_published_template_workspace(tmp_path)
    caller = _LocalGitCaller(repo=workspace, official_url=str(official))
    assert read_workspace_current_version(agent_address=AgentId.generate(), mngr_caller=caller) == _BASE_TAG
    fetch_head = workspace / ".git" / "FETCH_HEAD"
    assert fetch_head.exists()
    fetch_head.unlink()

    assert read_workspace_current_version(agent_address=AgentId.generate(), mngr_caller=caller) == _BASE_TAG

    assert not fetch_head.exists()


def test_a_workspace_with_an_update_self_marker_never_fetches(tmp_path: Path) -> None:
    """A workspace with a marker already has a version to report, so the read leaves its tags alone."""
    workspace, official = _make_published_template_workspace(tmp_path)
    run_git_for_backup_test(
        workspace, "commit", "-q", "--allow-empty", "-m", "update-self: merge upstream template (minds-v0.5.0)"
    )
    caller = _LocalGitCaller(repo=workspace, official_url=str(official))

    version = read_workspace_current_version(agent_address=AgentId.generate(), mngr_caller=caller)

    assert version == "minds-v0.5.0"
    assert _local_minds_tags(workspace) == []


def test_a_workspace_that_cannot_reach_the_official_template_reads_nothing_and_still_exits_clean(
    tmp_path: Path,
) -> None:
    """An offline workspace costs the read its attempt and nothing else: no version, no failed exec.

    The fetch's complaint must survive that clean exit: it is all that tells a
    read that could not reach the template from one with nothing to report.
    """
    unreachable_url = str(tmp_path / "not-a-repo.git")
    workspace, _official = _make_published_template_workspace(tmp_path)
    caller = _LocalGitCaller(repo=workspace, official_url=unreachable_url)

    version = read_workspace_current_version(agent_address=AgentId.generate(), mngr_caller=caller)

    assert version is None
    assert caller.shell_returncodes == [0]
    (stderr,) = caller.shell_stderrs
    assert unreachable_url in stderr


def _commit_on_day(repo: Path, day: int, *args: str) -> None:
    """Run a committing git command stamped on its own day.

    ``describe`` walks history in commit-date order, and a fixture whose
    commits all share one second can make it pick a farther tag.
    """
    stamp = f"2026-09-{day:02d}T12:00:00Z"
    run_git_for_backup_test(repo, *args, env_overrides={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})


@pytest.mark.witnesses("workspace-updates.updated-workspace-not-re-offered", partial="the version read only")
def test_a_run_landed_with_an_ordinary_merge_reads_as_the_release_it_landed(tmp_path: Path) -> None:
    """The shape a run leaves when its lead lands the worker branch with ``--no-ff`` instead of a fast-forward.

    The run's marker ends up on the landing merge's second parent, out of the
    first-parent marker read's reach, which finds the previous run's instead.
    The release the run merged is still reachable, so ``describe`` names it.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(workspace)], check=True, capture_output=True, timeout=60)
    _commit_on_day(workspace, 1, "commit", "-q", "--allow-empty", "-m", "upstream release")
    run_git_for_backup_test(workspace, "tag", "minds-v0.6.2")
    _commit_on_day(
        workspace, 2, "commit", "-q", "--allow-empty", "-m", "update-self: merge upstream template (minds-v0.6.2)"
    )
    run_git_for_backup_test(workspace, "checkout", "-q", "-b", "upstream", "minds-v0.6.2")
    _commit_on_day(workspace, 3, "commit", "-q", "--allow-empty", "-m", "next upstream release")
    run_git_for_backup_test(workspace, "tag", "minds-v0.7.0")
    run_git_for_backup_test(workspace, "checkout", "-q", "-b", "mngr/update-self", "main")
    _commit_on_day(
        workspace, 4, "merge", "-q", "--no-ff", "-m", "update-self: merge upstream template (minds-v0.7.0)", "upstream"
    )
    _commit_on_day(workspace, 5, "commit", "-q", "--allow-empty", "-m", "adapt the workspace to the release")
    run_git_for_backup_test(workspace, "checkout", "-q", "main")
    _commit_on_day(workspace, 6, "merge", "-q", "--no-ff", "--no-edit", "mngr/update-self")
    caller = _LocalGitCaller(repo=workspace, official_url=str(tmp_path / "unused.git"))

    version = read_workspace_current_version(agent_address=AgentId.generate(), mngr_caller=caller)

    assert version == "minds-v0.7.0"


@pytest.mark.witnesses("workspace-updates.version-not-recovered-for-an-unrelated-workspace")
def test_a_workspace_that_is_not_template_derived_never_fetches(tmp_path: Path) -> None:
    """A tree with no ``parent.toml`` is not the template's, so the read leaves its git alone.

    Measured against the real template before this guard existed: a repo
    sharing no history with it still took 70 MB of objects, after which
    ``describe`` answered nothing. ``parent.toml`` -- the template's own record
    of where the tree came from -- is the cheap local stand-in for "could a
    release tag ever describe this". It is deliberately conservative: this
    fixture's repo *would* have described after a fetch, and is skipped anyway.
    """
    workspace, official = _make_published_template_workspace(tmp_path)
    run_git_for_backup_test(workspace, "rm", "-q", "system/config/parent.toml")
    run_git_for_backup_test(workspace, "commit", "-q", "-m", "a repo of the user's own")
    caller = _LocalGitCaller(repo=workspace, official_url=str(official))

    version = read_workspace_current_version(agent_address=AgentId.generate(), mngr_caller=caller)

    assert version is None
    assert _local_minds_tags(workspace) == []
    # The other half of leaving its git alone: the guard is ahead of the
    # ``official``-remote write, the read's one other side effect.
    assert run_git_for_backup_test(workspace, "remote").split() == ["origin"]
    assert caller.shell_returncodes == [0]


def test_a_shallow_workspace_never_fetches(tmp_path: Path) -> None:
    """In a shallow clone the tags would land and ``describe`` still could not relate them to HEAD."""
    workspace, official = _make_published_template_workspace(tmp_path)
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", workspace.as_uri(), str(shallow)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    assert run_git_for_backup_test(shallow, "rev-parse", "--is-shallow-repository").strip() == "true"
    caller = _LocalGitCaller(repo=shallow, official_url=str(official))

    version = read_workspace_current_version(agent_address=AgentId.generate(), mngr_caller=caller)

    assert version is None
    assert _local_minds_tags(shallow) == []
    assert caller.shell_returncodes == [0]
