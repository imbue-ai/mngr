import json
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path

from pydantic import Field
from pydantic import PrivateAttr

from imbue.minds.desktop_client.workspace_version import parse_git_describe
from imbue.minds.desktop_client.workspace_version import parse_update_self_ref
from imbue.minds.desktop_client.workspace_version import parse_upgrade_merges
from imbue.minds.desktop_client.workspace_version import read_workspace_current_version
from imbue.minds.desktop_client.workspace_version import read_workspace_git_version
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId


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
        return MngrCallResult(returncode=0, stdout=json.dumps({"results": [{"stdout": self.stdout}]}))

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


def test_the_update_self_marker_outranks_the_tag() -> None:
    caller = _GitAnsweringCaller(stdout="update-self: merge upstream template (minds-v0.4.1)\nminds-v0.3.17\n")

    version = read_workspace_current_version(agent_id=AgentId.generate(), mngr_caller=caller)

    assert version == "minds-v0.4.1"


def test_the_marker_and_the_tag_are_read_in_one_exec() -> None:
    """Both git reads ride one ``mngr exec``, and a tagless clone must not fail it: ``describe`` is allowed to fail."""
    caller = _GitAnsweringCaller(stdout="minds-v0.4.1\n")

    read_workspace_current_version(agent_id=AgentId.generate(), mngr_caller=caller)

    assert len(caller.git_commands) == 1
    (command,) = caller.git_commands
    assert "--grep" in command
    assert "describe" in command
    assert command.endswith("|| true")


def test_a_workspace_with_no_marker_falls_back_to_the_tag() -> None:
    caller = _GitAnsweringCaller(stdout="minds-v0.4.1\n")

    version = read_workspace_current_version(agent_id=AgentId.generate(), mngr_caller=caller)

    assert version == "minds-v0.4.1"


def test_a_tagless_workspace_is_read_from_its_marker() -> None:
    """The create path checks the release out as a branch, so a fresh clone has no tags to describe."""
    caller = _GitAnsweringCaller(stdout="update-self: merge upstream template (minds-v0.4.1)\n")

    assert read_workspace_current_version(agent_id=AgentId.generate(), mngr_caller=caller) == "minds-v0.4.1"


def test_a_marker_the_strict_parse_rejects_does_not_shadow_the_tag() -> None:
    """The grep selects any subject that starts with the marker prefix; only an exact marker names a version."""
    caller = _GitAnsweringCaller(stdout="update-self: merge upstream template (minds-v0.4.1) [retry]\nminds-v0.3.17\n")

    assert read_workspace_current_version(agent_id=AgentId.generate(), mngr_caller=caller) == "minds-v0.3.17"


def test_a_workspace_with_neither_marker_nor_tag_has_no_version() -> None:
    assert read_workspace_current_version(agent_id=AgentId.generate(), mngr_caller=_GitAnsweringCaller()) is None


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
    read_workspace_git_version(agent_id=agent_id, mngr_caller=caller)
    assert len(caller.calls) == 2
    for argv in caller.calls:
        assert argv[0] == "exec"
        assert "--no-start" in argv
        assert "--" not in argv
        assert str(agent_id) in argv
        git_commands = [token for token in argv if token.startswith("git ")]
        assert len(git_commands) == 1


def test_version_read_parses_the_json_exec_envelope() -> None:
    """A successful exec's stdout is a ``--format json`` envelope; the command's
    own stdout must be extracted from it (raw human-format stdout would carry
    mngr's trailing ``Command succeeded on agent <name>`` status line).
    """
    envelope = json.dumps({"results": [{"stdout": "minds-v1.2.3\n"}]})
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=envelope))
    version = read_workspace_git_version(agent_id=AgentId.generate(), mngr_caller=caller)
    assert version.current_minds_version == "minds-v1.2.3"
