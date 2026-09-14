from pathlib import Path

import pytest

from imbue.mngr.api.preservation import PreservationManifest
from imbue.mngr.api.preservation import PreservationOutcome
from imbue.mngr.api.preservation import PreservedAgentArchive
from imbue.mngr.api.preservation import PreservedAgentIdentity
from imbue.mngr.api.preservation import PreservedItemResult
from imbue.mngr.api.trajectory import SUBAGENT_PROXY_PARENT_ID_LABEL
from imbue.mngr.api.trajectory import SUBAGENT_PROXY_TOOL_USE_ID_LABEL
from imbue.mngr.api.trajectory import _preserved_children_by_tool_call
from imbue.mngr.api.trajectory import _viable_preserved_children
from imbue.mngr.api.trajectory import find_one_preserved_archive
from imbue.mngr.api.trajectory import read_preserved_common_transcript
from imbue.mngr.errors import TrajectoryBuildError
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName

_PARENT_ID = AgentId("agent-00000000000000000000000000000001")
_ORIGIN_HOST_ID = HostId("host-00000000000000000000000000000001")
_OTHER_HOST_ID = HostId("host-00000000000000000000000000000002")
_TRANSCRIPT_REL_PATH = "events/claude/common_transcript"


def _identity(
    *,
    agent_name: str,
    agent_id: str,
    host_id: HostId = _ORIGIN_HOST_ID,
    host_name: str = "origin",
    provider_name: str = "local",
    labels: dict[str, str] | None = None,
) -> PreservedAgentIdentity:
    return PreservedAgentIdentity(
        host_id=host_id,
        host_name=HostName(host_name),
        provider_name=ProviderInstanceName(provider_name),
        agent_id=AgentId(agent_id),
        agent_name=AgentName(agent_name),
        agent_type=AgentTypeName("claude"),
        labels=labels or {},
    )


def _manifest(
    *,
    identity: PreservedAgentIdentity,
    items: tuple[PreservedItemResult, ...],
) -> PreservationManifest:
    return PreservationManifest(identity=identity, items=items)


def _transcript_item(outcome: PreservationOutcome, error: str | None = None) -> PreservedItemResult:
    return PreservedItemResult(
        rel_path=_TRANSCRIPT_REL_PATH,
        kind=FileType.DIRECTORY,
        outcome=outcome,
        error=error,
    )


def _archive(
    *,
    agent_name: str,
    agent_id: str,
    path: Path = Path("/preserved/archive"),
    identity: PreservedAgentIdentity | None = None,
    manifest: PreservationManifest | None = None,
) -> PreservedAgentArchive:
    return PreservedAgentArchive(
        path=path,
        agent_id=AgentId(agent_id),
        agent_name=AgentName(agent_name),
        identity=identity,
        manifest=manifest,
    )


def _copied_archive(
    *,
    agent_name: str,
    agent_id: str,
    path: Path = Path("/preserved/archive"),
    host_id: HostId = _ORIGIN_HOST_ID,
    host_name: str = "origin",
    provider_name: str = "local",
    labels: dict[str, str] | None = None,
) -> PreservedAgentArchive:
    """An archive whose manifest records a successfully copied common transcript."""
    identity = _identity(
        agent_name=agent_name,
        agent_id=agent_id,
        host_id=host_id,
        host_name=host_name,
        provider_name=provider_name,
        labels=labels,
    )
    return _archive(
        agent_name=agent_name,
        agent_id=agent_id,
        path=path,
        identity=identity,
        manifest=_manifest(identity=identity, items=(_transcript_item(PreservationOutcome.COPIED),)),
    )


def _unreadable_archive(*, agent_name: str, agent_id: str, path: Path) -> PreservedAgentArchive:
    """An archive whose manifest records that its common transcript never made it into the copy."""
    identity = _identity(agent_name=agent_name, agent_id=agent_id)
    return _archive(
        agent_name=agent_name,
        agent_id=agent_id,
        path=path,
        identity=identity,
        manifest=_manifest(identity=identity, items=(_transcript_item(PreservationOutcome.MISSING),)),
    )


def _child_labels(tool_use_id: str, parent_agent_id: AgentId = _PARENT_ID) -> dict[str, str]:
    return {
        SUBAGENT_PROXY_PARENT_ID_LABEL: str(parent_agent_id),
        SUBAGENT_PROXY_TOOL_USE_ID_LABEL: tool_use_id,
    }


def _write_archive_transcript(root: Path, *, agent_name: str, agent_id: str, content: str) -> Path:
    """Write an archive directory whose copied common transcript holds ``content``."""
    archive_path = root / f"{agent_name}--{agent_id}"
    transcript_dir = archive_path / _TRANSCRIPT_REL_PATH
    transcript_dir.mkdir(parents=True)
    (transcript_dir / "events.jsonl").write_text(content)
    return archive_path


# =============================================================================
# find_one_preserved_archive
# =============================================================================


def test_find_one_preserved_archive_resolves_by_name() -> None:
    wanted = _copied_archive(agent_name="wanted", agent_id="agent-00000000000000000000000000000011")
    other = _copied_archive(agent_name="other", agent_id="agent-00000000000000000000000000000012")

    resolved = find_one_preserved_archive(AgentAddress(agent=AgentName("wanted")), [other, wanted])

    assert resolved is wanted


def test_find_one_preserved_archive_resolves_by_id() -> None:
    wanted_id = "agent-00000000000000000000000000000013"
    wanted = _copied_archive(agent_name="shared-name", agent_id=wanted_id)
    other = _copied_archive(agent_name="shared-name", agent_id="agent-00000000000000000000000000000014")

    resolved = find_one_preserved_archive(AgentAddress(agent=AgentId(wanted_id)), [other, wanted])

    assert resolved is wanted


def test_find_one_preserved_archive_reports_no_match() -> None:
    archive = _copied_archive(agent_name="present", agent_id="agent-00000000000000000000000000000015")

    with pytest.raises(TrajectoryBuildError) as error:
        find_one_preserved_archive(AgentAddress(agent=AgentName("absent")), [archive])

    assert "No preserved agent archive matches 'absent'" in str(error.value)


def test_find_one_preserved_archive_rejects_an_ambiguous_name() -> None:
    first = _copied_archive(
        agent_name="twin",
        agent_id="agent-00000000000000000000000000000016",
        path=Path("/preserved/first"),
    )
    second = _copied_archive(
        agent_name="twin",
        agent_id="agent-00000000000000000000000000000017",
        path=Path("/preserved/second"),
        host_id=_OTHER_HOST_ID,
        host_name="elsewhere",
    )

    with pytest.raises(TrajectoryBuildError) as error:
        find_one_preserved_archive(AgentAddress(agent=AgentName("twin")), [first, second])

    message = str(error.value)
    assert "Multiple preserved agent archives match 'twin'" in message
    assert "/preserved/first" in message
    assert "/preserved/second" in message
    assert "adding a host qualifier where needed" in message


def test_find_one_preserved_archive_host_qualifier_selects_the_matching_origin() -> None:
    here = _copied_archive(
        agent_name="twin",
        agent_id="agent-00000000000000000000000000000018",
        host_name="origin",
    )
    elsewhere = _copied_archive(
        agent_name="twin",
        agent_id="agent-00000000000000000000000000000019",
        host_id=_OTHER_HOST_ID,
        host_name="elsewhere",
    )
    address = AgentAddress(agent=AgentName("twin"), host=HostAddress(host=HostName("origin")))

    assert find_one_preserved_archive(address, [here, elsewhere]) is here


def test_find_one_preserved_archive_host_qualifier_excludes_an_archive_without_provenance() -> None:
    archive = _archive(
        agent_name="legacy",
        agent_id="agent-00000000000000000000000000000020",
        path=Path("/preserved/legacy"),
    )
    address = AgentAddress(agent=AgentName("legacy"), host=HostAddress(host=HostName("origin")))

    with pytest.raises(TrajectoryBuildError) as error:
        find_one_preserved_archive(address, [archive])

    message = str(error.value)
    assert "No preserved agent archive matches 'legacy@origin'" in message
    assert "/preserved/legacy (recorded no origin host, so no qualifier can match it)" in message
    assert "Drop the host qualifier, or select the archive by agent id." in message


def test_find_one_preserved_archive_names_the_origin_a_host_qualifier_ruled_out() -> None:
    archive = _copied_archive(
        agent_name="elsewhere",
        agent_id="agent-00000000000000000000000000000029",
        path=Path("/preserved/elsewhere"),
        host_name="other-host",
        provider_name="modal",
    )
    address = AgentAddress(agent=AgentName("elsewhere"), host=HostAddress(host=HostName("origin")))

    with pytest.raises(TrajectoryBuildError) as error:
        find_one_preserved_archive(address, [archive])

    assert "/preserved/elsewhere (ran on other-host.modal)" in str(error.value)


def test_find_one_preserved_archive_ambiguity_asks_for_the_id_when_provenance_is_missing() -> None:
    """A host qualifier would exclude these archives rather than choose between them."""
    first = _archive(
        agent_name="twin",
        agent_id="agent-00000000000000000000000000000030",
        path=Path("/preserved/first"),
    )
    second = _archive(
        agent_name="twin",
        agent_id="agent-00000000000000000000000000000039",
        path=Path("/preserved/second"),
    )

    with pytest.raises(TrajectoryBuildError) as error:
        find_one_preserved_archive(AgentAddress(agent=AgentName("twin")), [first, second])

    message = str(error.value)
    assert "Select the agent id: some of these archives recorded no origin host" in message
    assert "adding a host qualifier" not in message


def test_find_one_preserved_archive_picks_the_only_archive_that_can_be_read() -> None:
    readable = _copied_archive(
        agent_name="twin",
        agent_id="agent-00000000000000000000000000000051",
        path=Path("/preserved/readable"),
    )
    unreadable = _unreadable_archive(
        agent_name="twin",
        agent_id="agent-00000000000000000000000000000052",
        path=Path("/preserved/unreadable"),
    )

    assert find_one_preserved_archive(AgentAddress(agent=AgentName("twin")), [unreadable, readable]) is readable


def test_find_one_preserved_archive_stays_ambiguous_when_no_match_can_be_read() -> None:
    first = _unreadable_archive(
        agent_name="twin",
        agent_id="agent-00000000000000000000000000000053",
        path=Path("/preserved/first"),
    )
    second = _unreadable_archive(
        agent_name="twin",
        agent_id="agent-00000000000000000000000000000054",
        path=Path("/preserved/second"),
    )

    with pytest.raises(TrajectoryBuildError) as error:
        find_one_preserved_archive(AgentAddress(agent=AgentName("twin")), [first, second])

    assert "Multiple preserved agent archives match 'twin'" in str(error.value)


def test_find_one_preserved_archive_returns_a_lone_match_that_cannot_be_read() -> None:
    """A single match is the caller's answer either way, so the read failure is what they hear about."""
    archive = _unreadable_archive(
        agent_name="lonely",
        agent_id="agent-00000000000000000000000000000055",
        path=Path("/preserved/lonely"),
    )

    assert find_one_preserved_archive(AgentAddress(agent=AgentName("lonely")), [archive]) is archive


# =============================================================================
# _preserved_children_by_tool_call
# =============================================================================


def _children_of_parent(archives: list[PreservedAgentArchive]) -> dict[str, list[PreservedAgentArchive]]:
    return _preserved_children_by_tool_call(
        parent_agent_id=_PARENT_ID,
        parent_host_id=_ORIGIN_HOST_ID,
        parent_provider_name=ProviderInstanceName("local"),
        archives=archives,
    )


def test_preserved_children_by_tool_call_groups_children_by_their_delegating_call() -> None:
    first = _copied_archive(
        agent_name="child-one",
        agent_id="agent-00000000000000000000000000000021",
        labels=_child_labels("toolu_one"),
    )
    second = _copied_archive(
        agent_name="child-two",
        agent_id="agent-00000000000000000000000000000022",
        labels=_child_labels("toolu_two"),
    )
    also_first = _copied_archive(
        agent_name="child-one-again",
        agent_id="agent-00000000000000000000000000000023",
        labels=_child_labels("toolu_one"),
    )

    children = _children_of_parent([first, second, also_first])

    assert children == {"toolu_one": [first, also_first], "toolu_two": [second]}


def test_preserved_children_by_tool_call_ignores_archives_from_another_origin() -> None:
    other_host = _copied_archive(
        agent_name="other-host-child",
        agent_id="agent-00000000000000000000000000000024",
        host_id=_OTHER_HOST_ID,
        labels=_child_labels("toolu_one"),
    )
    other_provider = _copied_archive(
        agent_name="other-provider-child",
        agent_id="agent-00000000000000000000000000000025",
        provider_name="modal",
        labels=_child_labels("toolu_one"),
    )

    assert _children_of_parent([other_host, other_provider]) == {}


def test_preserved_children_by_tool_call_ignores_archives_that_are_not_this_parents_children() -> None:
    other_parent = _copied_archive(
        agent_name="other-parents-child",
        agent_id="agent-00000000000000000000000000000026",
        labels=_child_labels("toolu_one", AgentId("agent-00000000000000000000000000000099")),
    )
    unlabeled = _copied_archive(
        agent_name="no-call-child",
        agent_id="agent-00000000000000000000000000000027",
        labels={SUBAGENT_PROXY_PARENT_ID_LABEL: str(_PARENT_ID)},
    )
    without_provenance = _archive(
        agent_name="legacy-child",
        agent_id="agent-00000000000000000000000000000028",
    )

    assert _children_of_parent([other_parent, unlabeled, without_provenance]) == {}


# =============================================================================
# _viable_preserved_children
# =============================================================================


def test_viable_preserved_children_keeps_an_archive_that_recorded_a_copied_transcript() -> None:
    archive = _copied_archive(agent_name="copied", agent_id="agent-00000000000000000000000000000031")

    viable, warnings = _viable_preserved_children("toolu_one", [archive])

    assert viable == [archive]
    assert warnings == []


def test_viable_preserved_children_keeps_an_archive_with_no_manifest() -> None:
    archive = _archive(agent_name="legacy", agent_id="agent-00000000000000000000000000000032")

    viable, warnings = _viable_preserved_children("toolu_one", [archive])

    assert viable == [archive]
    assert warnings == []


def test_viable_preserved_children_drops_an_archive_whose_transcript_was_not_copied() -> None:
    identity = _identity(agent_name="failed", agent_id="agent-00000000000000000000000000000033")
    archive = _archive(
        agent_name="failed",
        agent_id="agent-00000000000000000000000000000033",
        identity=identity,
        manifest=_manifest(
            identity=identity,
            items=(_transcript_item(PreservationOutcome.ERROR, error="rsync exited 23"),),
        ),
    )

    viable, warnings = _viable_preserved_children("toolu_one", [archive])

    assert viable == []
    assert warnings == [
        "Skipped preserved subagent 'failed' for tool call 'toolu_one': "
        "events/claude/common_transcript=error (rsync exited 23)"
    ]


def test_viable_preserved_children_says_when_a_manifest_requested_no_transcript_at_all() -> None:
    identity = _identity(agent_name="no-transcript", agent_id="agent-00000000000000000000000000000034")
    archive = _archive(
        agent_name="no-transcript",
        agent_id="agent-00000000000000000000000000000034",
        identity=identity,
        manifest=_manifest(
            identity=identity,
            items=(
                PreservedItemResult(
                    rel_path="logs/claude_transcript",
                    kind=FileType.DIRECTORY,
                    outcome=PreservationOutcome.COPIED,
                ),
            ),
        ),
    )

    viable, warnings = _viable_preserved_children("toolu_one", [archive])

    assert viable == []
    assert warnings == [
        "Skipped preserved subagent 'no-transcript' for tool call 'toolu_one': "
        "manifest records no common transcript item"
    ]


# =============================================================================
# read_preserved_common_transcript
# =============================================================================


def test_read_preserved_common_transcript_reads_the_copied_stream(tmp_path: Path) -> None:
    agent_id = "agent-00000000000000000000000000000041"
    archive_path = _write_archive_transcript(tmp_path, agent_name="copied", agent_id=agent_id, content='{"a": 1}\n')
    archive = _copied_archive(agent_name="copied", agent_id=agent_id, path=archive_path)

    path, content = read_preserved_common_transcript(archive)

    assert path == archive_path / _TRANSCRIPT_REL_PATH / "events.jsonl"
    assert content == '{"a": 1}\n'


def test_read_preserved_common_transcript_reads_an_archive_with_no_manifest(tmp_path: Path) -> None:
    agent_id = "agent-00000000000000000000000000000042"
    archive_path = _write_archive_transcript(tmp_path, agent_name="legacy", agent_id=agent_id, content='{"a": 2}\n')
    archive = _archive(agent_name="legacy", agent_id=agent_id, path=archive_path)

    _path, content = read_preserved_common_transcript(archive)

    assert content == '{"a": 2}\n'


def test_read_preserved_common_transcript_trusts_the_manifest_over_the_files_on_disk(tmp_path: Path) -> None:
    """A transcript the manifest did not record as copied is partial, so it is not read as if it were whole."""
    agent_id = "agent-00000000000000000000000000000043"
    archive_path = _write_archive_transcript(tmp_path, agent_name="partial", agent_id=agent_id, content="{}\n")
    raw_dir = archive_path / "logs" / "claude_transcript"
    raw_dir.mkdir(parents=True)
    identity = _identity(agent_name="partial", agent_id=agent_id)
    archive = _archive(
        agent_name="partial",
        agent_id=agent_id,
        path=archive_path,
        identity=identity,
        manifest=_manifest(
            identity=identity,
            items=(_transcript_item(PreservationOutcome.ERROR, error="host went away"),),
        ),
    )

    with pytest.raises(TrajectoryBuildError) as error:
        read_preserved_common_transcript(archive)

    message = str(error.value)
    assert "has no copied common transcript" in message
    assert "events/claude/common_transcript=error (host went away)" in message
    assert str(raw_dir) in message


def test_read_preserved_common_transcript_reports_a_missing_stream_and_no_raw_transcript(tmp_path: Path) -> None:
    agent_id = "agent-00000000000000000000000000000044"
    archive_path = tmp_path / f"empty--{agent_id}"
    archive_path.mkdir(parents=True)
    archive = _archive(agent_name="empty", agent_id=agent_id, path=archive_path)

    with pytest.raises(TrajectoryBuildError) as error:
        read_preserved_common_transcript(archive)

    assert "no native raw transcript directory was found" in str(error.value)


def test_read_preserved_common_transcript_refuses_to_choose_between_two_streams(tmp_path: Path) -> None:
    agent_id = "agent-00000000000000000000000000000045"
    archive_path = _write_archive_transcript(tmp_path, agent_name="two", agent_id=agent_id, content="{}\n")
    second_dir = archive_path / "events" / "codex" / "common_transcript"
    second_dir.mkdir(parents=True)
    (second_dir / "events.jsonl").write_text("{}\n")
    archive = _archive(agent_name="two", agent_id=agent_id, path=archive_path)

    with pytest.raises(TrajectoryBuildError) as error:
        read_preserved_common_transcript(archive)

    assert "has multiple common transcripts" in str(error.value)
