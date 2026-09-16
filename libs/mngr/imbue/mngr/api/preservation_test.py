import json
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.api.preservation import PRESERVATION_MANIFEST_FILENAME
from imbue.mngr.api.preservation import PRESERVATION_MANIFEST_SCHEMA_VERSION
from imbue.mngr.api.preservation import PreservationManifest
from imbue.mngr.api.preservation import PreservationOutcome
from imbue.mngr.api.preservation import PreservedItem
from imbue.mngr.api.preservation import PreservedItemResult
from imbue.mngr.api.preservation import TranscriptFlushOutcome
from imbue.mngr.api.preservation import TranscriptFlushResult
from imbue.mngr.api.preservation import adopt_sessions
from imbue.mngr.api.preservation import build_transcript_preserved_items
from imbue.mngr.api.preservation import dedupe_by_resolved_path
from imbue.mngr.api.preservation import discover_preserved_agent_archives
from imbue.mngr.api.preservation import flag_gated_items
from imbue.mngr.api.preservation import get_local_preserved_agent_dir
from imbue.mngr.api.preservation import get_local_preserved_agent_dir_for_host
from imbue.mngr.api.preservation import get_preserved_agent_dir
from imbue.mngr.api.preservation import preserve_agent_data
from imbue.mngr.api.preservation import preserve_agent_state
from imbue.mngr.api.preservation import preserve_host_agents_on_destroy
from imbue.mngr.api.preservation import preserved_agent_matches
from imbue.mngr.api.preservation import require_unique_match
from imbue.mngr.api.preservation import run_adopt_session_preflight
from imbue.mngr.api.preservation import write_preservation_manifest
from imbue.mngr.config.agent_config_registry import resolve_agent_type
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.host import get_agent_state_dir_path
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.hosts.offline_host import OfflineHostWithVolume
from imbue.mngr.hosts.offline_host import make_readable_offline_host
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin
from imbue.mngr.interfaces.agent import HasTranscriptMixin
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.interfaces.data_types import VolumeFile
from imbue.mngr.interfaces.host import HostFileReadInterface
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.testing import allow_warnings


class _TranscriptPreservationAgent(BaseAgent, HasTranscriptMixin):
    def get_raw_transcript_scripts(self) -> Mapping[str, str]:
        return {"stream_transcript.sh": ""}


class _CommonTranscriptPreservationAgent(_TranscriptPreservationAgent, HasCommonTranscriptMixin):
    @property
    def is_common_transcript_enabled(self) -> bool:
        return True

    def get_common_transcript_scripts(self) -> Mapping[str, str]:
        return {"common_transcript.sh": ""}


def _make_local_host(local_provider: LocalProviderInstance) -> Host:
    """The local provider's online host, which the destruction-time paths preserve from."""
    host = local_provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)
    return host


def _make_local_agent(
    local_provider: LocalProviderInstance,
    host: Host,
    agent_name: AgentName,
    agent_id: AgentId,
    *,
    agent_class: type[BaseAgent] = _TranscriptPreservationAgent,
) -> BaseAgent:
    """An agent on ``host``, for the tests that drive preserve_agent_state directly.

    The state dir is the caller's to populate: what each test plants there is the whole
    difference between them.
    """
    return agent_class(
        id=agent_id,
        name=agent_name,
        agent_type=AgentTypeName("generic"),
        work_dir=host.host_dir / "worktree",
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        host=host,
        mngr_ctx=local_provider.mngr_ctx,
        agent_config=AgentTypeConfig(command=CommandString("true")),
    )


def _claude_like_items() -> list[PreservedItem]:
    return [
        PreservedItem(rel_path="plugin/claude/anthropic/projects", kind=FileType.DIRECTORY),
        PreservedItem(rel_path="logs/claude_transcript", kind=FileType.DIRECTORY),
        PreservedItem(rel_path="claude_session_id_history", kind=FileType.FILE),
        # An item that does not exist on the source -- must be skipped silently.
        PreservedItem(rel_path="does/not/exist", kind=FileType.DIRECTORY),
    ]


def _populate_state_dir(state_dir: Path) -> None:
    """Write a representative set of files into an agent state directory."""
    (state_dir / "plugin" / "claude" / "anthropic" / "projects" / "proj").mkdir(parents=True, exist_ok=True)
    (state_dir / "plugin" / "claude" / "anthropic" / "projects" / "proj" / "session.jsonl").write_text(
        '{"event": 1}\n'
    )
    (state_dir / "logs" / "claude_transcript").mkdir(parents=True, exist_ok=True)
    (state_dir / "logs" / "claude_transcript" / "events.jsonl").write_text("raw\n")
    (state_dir / "claude_session_id_history").write_text("sess-1\nsess-2\n")


def _assert_mirrored(dest_root: Path) -> None:
    """Assert the preserved files mirror the agent-state-dir layout verbatim."""
    projects_file = dest_root / "plugin" / "claude" / "anthropic" / "projects" / "proj" / "session.jsonl"
    assert projects_file.read_text() == '{"event": 1}\n'
    assert (dest_root / "logs" / "claude_transcript" / "events.jsonl").read_text() == "raw\n"
    assert (dest_root / "claude_session_id_history").read_text() == "sess-1\nsess-2\n"
    # The non-existent item must not have produced anything.
    assert not (dest_root / "does").exists()


def test_get_preserved_agent_dir_layout() -> None:
    agent_id = AgentId.generate()
    path = get_preserved_agent_dir(Path("/host"), AgentName("amy"), agent_id)
    assert path == Path(f"/host/preserved/amy--{agent_id}")


def test_host_aware_preserved_dir_avoids_known_cross_host_collision(temp_mngr_ctx: MngrContext) -> None:
    agent_id = AgentId.generate()
    agent_name = AgentName("amy")
    first_host_id = HostId.generate()
    second_host_id = HostId.generate()
    legacy = get_local_preserved_agent_dir(temp_mngr_ctx, agent_name, agent_id)
    legacy.mkdir(parents=True)
    (legacy / "data.json").write_text(
        json.dumps({"id": str(agent_id), "name": str(agent_name), "type": "codex", "labels": {}})
    )
    (legacy / "mngr_usage_meta.json").write_text(
        json.dumps({"provider_name": "local", "host_id": str(first_host_id), "host_name": "first"})
    )

    assert get_local_preserved_agent_dir_for_host(temp_mngr_ctx, agent_name, agent_id, first_host_id) == legacy
    assert get_local_preserved_agent_dir_for_host(
        temp_mngr_ctx, agent_name, agent_id, second_host_id
    ) == legacy.with_name(f"{legacy.name}--{second_host_id}")


@pytest.mark.rsync
def test_preserve_agent_data_online_mirrors_layout(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """Preserving from an online (local) host copies files to a mirrored layout."""
    state_dir = tmp_path / "state"
    _populate_state_dir(state_dir)
    dest_root = tmp_path / "dest"

    results = preserve_agent_data(_claude_like_items(), local_host, state_dir, dest_root, temp_mngr_ctx)

    _assert_mirrored(dest_root)
    assert [result.outcome for result in results] == ["copied", "copied", "copied", "missing"]


@pytest.mark.rsync
@pytest.mark.allow_warnings
def test_preserve_agent_state_copies_flush_output_even_when_producer_fails(
    local_provider: LocalProviderInstance,
) -> None:
    host = _make_local_host(local_provider)
    agent_id = AgentId.generate()
    agent_name = AgentName("flush-before-copy")
    state_dir = get_agent_state_dir_path(host.host_dir, agent_id)
    commands_dir = state_dir / "commands"
    commands_dir.mkdir(parents=True)
    (state_dir / "data.json").write_text(
        json.dumps(
            {
                "id": str(agent_id),
                "name": str(agent_name),
                "type": "generic",
                "labels": {},
            }
        )
    )
    (state_dir / "env").write_text(f'export MNGR_AGENT_STATE_DIR="{state_dir}"\n')
    script = commands_dir / "stream_transcript.sh"
    script.write_text(
        "#!/bin/sh\n"
        'mkdir -p "$MNGR_AGENT_STATE_DIR/logs/generic_transcript"\n'
        'echo final-bytes > "$MNGR_AGENT_STATE_DIR/logs/generic_transcript/events.jsonl"\n'
        "exit 7\n"
    )
    script.chmod(0o755)
    agent = _make_local_agent(local_provider, host, agent_name, agent_id)

    preserve_agent_state(
        [PreservedItem(rel_path="logs/generic_transcript", kind=FileType.DIRECTORY)],
        agent,
        host,
    )

    dest = get_local_preserved_agent_dir(local_provider.mngr_ctx, agent_name, agent_id)
    assert (dest / "logs/generic_transcript/events.jsonl").read_text() == "final-bytes\n"
    manifest = PreservationManifest.model_validate_json((dest / PRESERVATION_MANIFEST_FILENAME).read_text())
    assert [(result.script_name, result.outcome) for result in manifest.transcript_flush] == [
        ("stream_transcript.sh", TranscriptFlushOutcome.ERROR)
    ]
    assert manifest.transcript_flush[0].error
    assert [(item.rel_path, item.outcome) for item in manifest.items] == [("logs/generic_transcript", "copied")]


@pytest.mark.rsync
def test_preserve_agent_state_flushes_the_common_transcript_producer_too(
    local_provider: LocalProviderInstance,
) -> None:
    """The common transcript is what a preserved archive is read back through, so its final pass runs."""
    host = _make_local_host(local_provider)
    agent_id = AgentId.generate()
    agent_name = AgentName("flush-both-producers")
    state_dir = get_agent_state_dir_path(host.host_dir, agent_id)
    commands_dir = state_dir / "commands"
    commands_dir.mkdir(parents=True)
    (state_dir / "env").write_text(f'export MNGR_AGENT_STATE_DIR="{state_dir}"\n')
    for script_name, out_rel in (
        ("stream_transcript.sh", "logs/generic_transcript"),
        ("common_transcript.sh", "events/generic/common_transcript"),
    ):
        script = commands_dir / script_name
        script.write_text(
            "#!/bin/sh\n"
            f'mkdir -p "$MNGR_AGENT_STATE_DIR/{out_rel}"\n'
            f'echo {script_name} > "$MNGR_AGENT_STATE_DIR/{out_rel}/events.jsonl"\n'
        )
        script.chmod(0o755)
    agent = _make_local_agent(
        local_provider, host, agent_name, agent_id, agent_class=_CommonTranscriptPreservationAgent
    )

    preserve_agent_state(build_transcript_preserved_items("generic"), agent, host)

    dest = get_local_preserved_agent_dir(local_provider.mngr_ctx, agent_name, agent_id)
    manifest = PreservationManifest.model_validate_json((dest / PRESERVATION_MANIFEST_FILENAME).read_text())
    assert [(result.script_name, result.outcome) for result in manifest.transcript_flush] == [
        ("stream_transcript.sh", TranscriptFlushOutcome.SUCCEEDED),
        ("common_transcript.sh", TranscriptFlushOutcome.SUCCEEDED),
    ]
    # Each producer's final bytes reached the archive, so the flush ran before the copy.
    assert (dest / "logs/generic_transcript/events.jsonl").read_text() == "stream_transcript.sh\n"
    assert (dest / "events/generic/common_transcript/events.jsonl").read_text() == "common_transcript.sh\n"


def test_preserve_agent_state_records_provenance_without_a_discovery_record(
    local_provider: LocalProviderInstance,
) -> None:
    """An agent the host cannot discover still gets a manifest naming its origin host."""
    host = _make_local_host(local_provider)
    agent_id = AgentId.generate()
    agent_name = AgentName("no-discovery-record")
    state_dir = get_agent_state_dir_path(host.host_dir, agent_id)
    state_dir.mkdir(parents=True)
    (state_dir / _SESSION_HISTORY_REL_PATH).write_text("sid\n")
    agent = _make_local_agent(local_provider, host, agent_name, agent_id, agent_class=BaseAgent)
    assert [ref.agent_id for ref in host.discover_agents()] == []

    preserve_agent_state(_SESSION_HISTORY_ITEMS, agent, host)

    dest = get_local_preserved_agent_dir(local_provider.mngr_ctx, agent_name, agent_id)
    manifest = PreservationManifest.model_validate_json((dest / PRESERVATION_MANIFEST_FILENAME).read_text())
    assert manifest.identity.provider_name == host.get_provider_name()
    assert manifest.identity.host_id == host.id
    assert manifest.identity.agent_id == agent_id
    assert [(item.rel_path, item.outcome) for item in manifest.items] == [(_SESSION_HISTORY_REL_PATH, "copied")]


def _make_offline_with_volume(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> OfflineHostWithVolume:
    offline = OfflineHost(
        id=local_provider.host_id,
        provider_instance=local_provider,
        mngr_ctx=temp_mngr_ctx,
        certified_host_data=CertifiedHostData(
            host_id=str(local_provider.host_id),
            host_name="local",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        ),
    )
    readable = make_readable_offline_host(offline)
    assert isinstance(readable, OfflineHostWithVolume), "local provider should expose a readable volume"
    return readable


def test_offline_host_with_volume_reads_via_host_dir_paths(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """OfflineHostWithVolume reads files addressed by absolute paths under host_dir."""
    host = _make_offline_with_volume(local_provider, temp_mngr_ctx)
    agent_id = AgentId.generate()
    state_dir = host.host_dir / "agents" / str(agent_id)
    _populate_state_dir(state_dir)

    assert host.read_file(state_dir / "claude_session_id_history") == b"sess-1\nsess-2\n"
    assert host.read_text_file(state_dir / "claude_session_id_history") == "sess-1\nsess-2\n"
    assert host.path_exists(state_dir / "logs" / "claude_transcript")
    assert not host.path_exists(state_dir / "nope")

    listed = host.list_directory(state_dir / "logs" / "claude_transcript", recursive=True)
    listed_paths = {entry.path for entry in listed}
    assert str(state_dir / "logs" / "claude_transcript" / "events.jsonl") in listed_paths
    assert all(Path(entry.path).is_absolute() for entry in listed)

    history_path = state_dir / "claude_session_id_history"
    mtime = host.get_file_mtime(history_path)
    assert isinstance(mtime, datetime)
    # The reported mtime must match the file's real on-disk mtime (within the
    # one-second resolution of the volume listing's integer timestamp), not just
    # be some datetime.
    real_mtime = datetime.fromtimestamp(history_path.stat().st_mtime, tz=timezone.utc)
    assert abs((mtime - real_mtime).total_seconds()) <= 1.0
    # A non-existent file yields None (the parent-listing scan falls through).
    assert host.get_file_mtime(state_dir / "no_such_file") is None


class _OneFileFailingReader(HostFileReadInterface):
    """A reader where every declared file "exists" but reading one path raises.

    Exercises ``preserve_agent_data``'s per-item failure isolation: one item's
    read failure must be swallowed (logged) without aborting the remaining items
    or the destruction that triggered preservation.
    """

    contents_by_path: dict[str, bytes]
    failing_path: str

    def read_file(self, path: Path) -> bytes:
        key = str(path)
        if key == self.failing_path:
            raise OSError("simulated read failure")
        return self.contents_by_path[key]

    def read_text_file(self, path: Path, encoding: str = "utf-8") -> str:
        return self.read_file(path).decode(encoding)

    def path_exists(self, path: Path) -> bool:
        return str(path) == self.failing_path or str(path) in self.contents_by_path

    def get_file_mtime(self, path: Path) -> datetime | None:
        return None

    def list_directory(self, path: Path, *, recursive: bool = False) -> list[VolumeFile]:
        return []


@pytest.mark.allow_warnings
def test_preserve_agent_data_isolates_per_item_read_failures(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """A single item that fails to read is skipped (warned), not fatal to the rest."""
    state_dir = Path("/state")
    items = [
        PreservedItem(rel_path="good_before", kind=FileType.FILE),
        PreservedItem(rel_path="bad", kind=FileType.FILE),
        PreservedItem(rel_path="good_after", kind=FileType.FILE),
    ]
    reader = _OneFileFailingReader(
        contents_by_path={
            str(state_dir / "good_before"): b"before\n",
            str(state_dir / "good_after"): b"after\n",
        },
        failing_path=str(state_dir / "bad"),
    )
    dest_root = tmp_path / "dest"

    # Must not raise even though the middle item's read fails.
    results = preserve_agent_data(items, reader, state_dir, dest_root, temp_mngr_ctx)

    # The items on either side of the failure are still preserved.
    assert (dest_root / "good_before").read_text() == "before\n"
    assert (dest_root / "good_after").read_text() == "after\n"
    # The failing item produced nothing.
    assert not (dest_root / "bad").exists()
    # ...and the attempt records what each item observed, including what the failure reported.
    assert [(result.rel_path, result.outcome) for result in results] == [
        ("good_before", PreservationOutcome.COPIED),
        ("bad", PreservationOutcome.ERROR),
        ("good_after", PreservationOutcome.COPIED),
    ]
    assert results[1].error == "simulated read failure"


def test_preserve_agent_data_offline_mirrors_layout(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """Preserving from a volume-backed offline host walks the volume and mirrors layout."""
    host = _make_offline_with_volume(local_provider, temp_mngr_ctx)
    agent_id = AgentId.generate()
    state_dir = host.host_dir / "agents" / str(agent_id)
    _populate_state_dir(state_dir)
    dest_root = tmp_path / "offline_dest"

    # The offline host is a read-only file reader, not an online host.
    assert isinstance(host, HostFileReadInterface)

    preserve_agent_data(_claude_like_items(), host, state_dir, dest_root, temp_mngr_ctx)

    _assert_mirrored(dest_root)


def test_build_transcript_preserved_items_follows_convention() -> None:
    """The raw and common transcript directories follow the logs/ and events/ convention."""
    items = build_transcript_preserved_items("codex")
    assert items == [
        PreservedItem(rel_path="logs/codex_transcript", kind=FileType.DIRECTORY),
        PreservedItem(rel_path="events/codex/common_transcript", kind=FileType.DIRECTORY),
    ]


_SESSION_HISTORY_REL_PATH: str = "root_session"
_SESSION_HISTORY_ITEMS: list[PreservedItem] = [PreservedItem(rel_path=_SESSION_HISTORY_REL_PATH, kind=FileType.FILE)]


def _make_discovered_agent(certified_data: dict[str, object]) -> DiscoveredAgent:
    return DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("test"),
        provider_name=ProviderInstanceName("local"),
        certified_data=certified_data,
    )


@pytest.mark.parametrize(
    "agent_config, expected",
    [
        ({"preserve_on_destroy": True}, _SESSION_HISTORY_ITEMS),
        ({"preserve_on_destroy": False}, None),
        ({}, None),
    ],
)
def test_flag_gated_items_returns_items_only_when_opted_in(
    agent_config: dict[str, object], expected: list[PreservedItem] | None
) -> None:
    """The items are returned only when the named flag is truthy; missing/false yields None."""
    ref = _make_discovered_agent({"type": "codex", "agent_config": agent_config})
    assert flag_gated_items(ref, "preserve_on_destroy", _SESSION_HISTORY_ITEMS) == expected


def _items_when_opted_in(ref: DiscoveredAgent) -> Sequence[PreservedItem] | None:
    return flag_gated_items(ref, "preserve_on_destroy", _SESSION_HISTORY_ITEMS)


def _write_agent_record_and_session(
    host: Host, agent_type: str, *, preserve_on_destroy: bool
) -> tuple[AgentName, AgentId]:
    """Write a discoverable agent (data.json) plus its session-history file under the host dir."""
    agent_id = AgentId.generate()
    agent_name = AgentName(f"agent-{agent_type}-{agent_id}")
    state_dir = get_agent_state_dir_path(host.host_dir, agent_id)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "data.json").write_text(
        json.dumps(
            {
                "id": str(agent_id),
                "name": str(agent_name),
                "type": agent_type,
                "agent_config": {"preserve_on_destroy": preserve_on_destroy},
            }
        )
    )
    (state_dir / _SESSION_HISTORY_REL_PATH).write_text("sid\n")
    return agent_name, agent_id


def test_preserve_host_agents_on_destroy_filters_by_type_and_flag(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Only agents of the requested type whose config opted in are preserved.

    Exercises the real online host's ``discover_agents`` (reading the data.json
    records written under its host dir) plus the type filter and opt-in gating.
    """
    opted_in_name, opted_in_id = _write_agent_record_and_session(local_host, "codex", preserve_on_destroy=True)
    opted_out_name, opted_out_id = _write_agent_record_and_session(local_host, "codex", preserve_on_destroy=False)
    other_type_name, other_type_id = _write_agent_record_and_session(local_host, "claude", preserve_on_destroy=True)

    preserve_host_agents_on_destroy(local_host, temp_mngr_ctx, AgentTypeName("codex"), _items_when_opted_in)

    preserved_in = get_local_preserved_agent_dir(temp_mngr_ctx, opted_in_name, opted_in_id)
    preserved_out = get_local_preserved_agent_dir(temp_mngr_ctx, opted_out_name, opted_out_id)
    preserved_other = get_local_preserved_agent_dir(temp_mngr_ctx, other_type_name, other_type_id)

    # The codex agent that opted in is preserved...
    assert (preserved_in / _SESSION_HISTORY_REL_PATH).read_text() == "sid\n"
    manifest = PreservationManifest.model_validate_json((preserved_in / PRESERVATION_MANIFEST_FILENAME).read_text())
    assert manifest.identity.host_id == local_host.id
    assert manifest.identity.host_name == local_host.get_name()
    assert manifest.identity.agent_id == opted_in_id
    assert manifest.identity.agent_name == opted_in_name
    assert manifest.identity.agent_type == AgentTypeName("codex")
    assert manifest.identity.labels == {}
    assert [(item.rel_path, item.outcome) for item in manifest.items] == [(_SESSION_HISTORY_REL_PATH, "copied")]
    # ...the codex agent that opted out is skipped...
    assert not preserved_out.exists()
    # ...and the claude agent is skipped by the type filter despite opting in.
    assert not preserved_other.exists()


def test_discover_preserved_agent_archives_reads_manifests_and_legacy_archives(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
) -> None:
    manifested_name, manifested_id = _write_agent_record_and_session(local_host, "codex", preserve_on_destroy=True)
    preserve_host_agents_on_destroy(local_host, temp_mngr_ctx, AgentTypeName("codex"), _items_when_opted_in)

    root = Path(temp_mngr_ctx.config.default_host_dir) / "preserved"
    legacy_id = AgentId.generate()
    legacy_name = AgentName("old--agent-helper")
    legacy_path = root / f"{legacy_name}--{legacy_id}"
    legacy_path.mkdir(parents=True)

    archives = discover_preserved_agent_archives(Path(temp_mngr_ctx.config.default_host_dir))
    by_id = {archive.agent_id: archive for archive in archives}
    assert by_id[manifested_id].identity is not None
    assert by_id[manifested_id].manifest is not None
    assert by_id[legacy_id].agent_name == legacy_name
    assert by_id[legacy_id].identity is None
    assert by_id[legacy_id].manifest is None

    assert [archive.path for archive in archives if preserved_agent_matches(archive, manifested_name)] == [
        by_id[manifested_id].path
    ]


def test_discover_preserved_agent_archives_does_not_treat_invalid_manifest_as_legacy(
    temp_mngr_ctx: MngrContext,
) -> None:
    agent_id = AgentId.generate()
    agent_name = AgentName("invalid-manifest")
    root = Path(temp_mngr_ctx.config.default_host_dir) / "preserved"
    archive = root / f"{agent_name}--{agent_id}"
    archive.mkdir(parents=True)
    (archive / PRESERVATION_MANIFEST_FILENAME).write_text("not json")

    with allow_warnings(match="Ignoring invalid preservation manifest"):
        assert discover_preserved_agent_archives(root.parent) == []


def test_discover_preserved_agent_archives_reports_an_unreadable_manifest(tmp_path: Path) -> None:
    """A manifest that cannot be read at all fails the whole listing rather than leaking an OSError."""
    archive = tmp_path / "preserved" / f"broken--{AgentId.generate()}"
    (archive / PRESERVATION_MANIFEST_FILENAME).mkdir(parents=True)

    with pytest.raises(MngrError, match="Could not read preservation manifest"):
        discover_preserved_agent_archives(tmp_path)


def test_preserved_agent_matches_does_not_match_id_shaped_name_as_id(temp_mngr_ctx: MngrContext) -> None:
    id_shaped_name = AgentName(str(AgentId.generate()))
    actual_id = AgentId.generate()
    root = Path(temp_mngr_ctx.config.default_host_dir) / "preserved"
    archive_path = root / f"{id_shaped_name}--{actual_id}"
    archive_path.mkdir(parents=True)
    (archive,) = discover_preserved_agent_archives(root.parent)

    assert not preserved_agent_matches(archive, AgentId(str(id_shaped_name)))
    assert preserved_agent_matches(archive, id_shaped_name)
    assert archive.path == archive_path


def test_discover_preserved_agent_archives_rejects_non_directory_root(tmp_path: Path) -> None:
    root = tmp_path / "preserved"
    root.write_text("not a directory")

    with pytest.raises(MngrError, match=f"Preserved agent archive root is not a directory: {root}"):
        discover_preserved_agent_archives(tmp_path)


def test_repeated_preservation_merges_item_results(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
) -> None:
    agent_name, agent_id = _write_agent_record_and_session(local_host, "codex", preserve_on_destroy=True)
    state_dir = get_agent_state_dir_path(local_host.host_dir, agent_id)
    (state_dir / "second").write_text("second")

    preserve_host_agents_on_destroy(local_host, temp_mngr_ctx, AgentTypeName("codex"), _items_when_opted_in)
    preserve_host_agents_on_destroy(
        local_host,
        temp_mngr_ctx,
        AgentTypeName("codex"),
        lambda _ref: [PreservedItem(rel_path="second", kind=FileType.FILE)],
    )

    dest = get_local_preserved_agent_dir(temp_mngr_ctx, agent_name, agent_id)
    manifest = PreservationManifest.model_validate_json((dest / PRESERVATION_MANIFEST_FILENAME).read_text())
    assert [(item.rel_path, item.outcome) for item in manifest.items] == [
        (_SESSION_HISTORY_REL_PATH, "copied"),
        ("second", "copied"),
    ]


def test_preservation_creates_no_archive_when_nothing_was_there_to_preserve(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
) -> None:
    """An attempt that finds nothing leaves no empty archive to answer later name lookups."""
    agent_name, agent_id = _write_agent_record_and_session(local_host, "codex", preserve_on_destroy=True)
    (get_agent_state_dir_path(local_host.host_dir, agent_id) / _SESSION_HISTORY_REL_PATH).unlink()

    preserve_host_agents_on_destroy(local_host, temp_mngr_ctx, AgentTypeName("codex"), _items_when_opted_in)

    assert not get_local_preserved_agent_dir(temp_mngr_ctx, agent_name, agent_id).exists()
    assert discover_preserved_agent_archives(Path(temp_mngr_ctx.config.default_host_dir)) == []


def test_preservation_creates_no_archive_when_only_producer_passes_were_skipped(
    local_provider: LocalProviderInstance,
) -> None:
    """A skipped producer pass is not content, so it does not justify an archive on its own."""
    host = _make_local_host(local_provider)
    agent_id = AgentId.generate()
    agent_name = AgentName("nothing-to-preserve")
    state_dir = get_agent_state_dir_path(host.host_dir, agent_id)
    state_dir.mkdir(parents=True)
    (state_dir / "env").write_text(f'export MNGR_AGENT_STATE_DIR="{state_dir}"\n')
    agent = _make_local_agent(local_provider, host, agent_name, agent_id)

    preserve_agent_state(_SESSION_HISTORY_ITEMS, agent, host)

    assert not get_local_preserved_agent_dir(local_provider.mngr_ctx, agent_name, agent_id).exists()


def test_manifest_merge_does_not_downgrade_a_copied_item_to_missing(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
) -> None:
    agent_name, agent_id = _write_agent_record_and_session(local_host, "codex", preserve_on_destroy=True)
    preserve_host_agents_on_destroy(local_host, temp_mngr_ctx, AgentTypeName("codex"), _items_when_opted_in)

    # A second attempt after the source item is gone knows less than the first one did.
    (get_agent_state_dir_path(local_host.host_dir, agent_id) / _SESSION_HISTORY_REL_PATH).unlink()
    preserve_host_agents_on_destroy(local_host, temp_mngr_ctx, AgentTypeName("codex"), _items_when_opted_in)

    dest = get_local_preserved_agent_dir(temp_mngr_ctx, agent_name, agent_id)
    manifest = PreservationManifest.model_validate_json((dest / PRESERVATION_MANIFEST_FILENAME).read_text())
    assert [(item.rel_path, item.outcome) for item in manifest.items] == [(_SESSION_HISTORY_REL_PATH, "copied")]
    assert (dest / _SESSION_HISTORY_REL_PATH).read_text() == "sid\n"


def test_manifest_merge_replaces_a_copied_item_with_a_later_error(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
) -> None:
    """A copy that failed part-way may have disturbed the archive, so its error is what stands."""
    agent_name, agent_id = _write_agent_record_and_session(local_host, "codex", preserve_on_destroy=True)
    preserve_host_agents_on_destroy(local_host, temp_mngr_ctx, AgentTypeName("codex"), _items_when_opted_in)
    dest = get_local_preserved_agent_dir(temp_mngr_ctx, agent_name, agent_id)
    existing = PreservationManifest.model_validate_json((dest / PRESERVATION_MANIFEST_FILENAME).read_text())

    write_preservation_manifest(
        dest,
        PreservationManifest(
            identity=existing.identity,
            items=(
                PreservedItemResult(
                    rel_path=_SESSION_HISTORY_REL_PATH,
                    kind=FileType.FILE,
                    outcome=PreservationOutcome.ERROR,
                    error="copy failed part-way",
                ),
            ),
        ),
    )

    merged = PreservationManifest.model_validate_json((dest / PRESERVATION_MANIFEST_FILENAME).read_text())
    assert [(item.rel_path, item.outcome, item.error) for item in merged.items] == [
        (_SESSION_HISTORY_REL_PATH, PreservationOutcome.ERROR, "copy failed part-way")
    ]


def test_manifest_merge_retains_incoming_attempt_time_and_prior_flush(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
) -> None:
    agent_name, agent_id = _write_agent_record_and_session(local_host, "codex", preserve_on_destroy=True)
    preserve_host_agents_on_destroy(local_host, temp_mngr_ctx, AgentTypeName("codex"), _items_when_opted_in)
    dest = get_local_preserved_agent_dir(temp_mngr_ctx, agent_name, agent_id)
    existing = PreservationManifest.model_validate_json((dest / PRESERVATION_MANIFEST_FILENAME).read_text())
    first = PreservationManifest(
        preserved_at=existing.preserved_at,
        identity=existing.identity,
        items=existing.items,
        transcript_flush=(
            TranscriptFlushResult(
                script_name="common_transcript.sh",
                outcome=TranscriptFlushOutcome.SUCCEEDED,
            ),
        ),
    )
    write_preservation_manifest(dest, first)

    incoming_time = datetime(2030, 1, 2, tzinfo=timezone.utc)
    incoming = PreservationManifest(preserved_at=incoming_time, identity=existing.identity, items=())
    write_preservation_manifest(dest, incoming)

    merged = PreservationManifest.model_validate_json((dest / PRESERVATION_MANIFEST_FILENAME).read_text())
    assert merged.preserved_at == incoming_time
    # The carried-forward pass keeps the time it actually ran, not the later attempt's.
    assert merged.transcript_flush == first.transcript_flush


@pytest.mark.parametrize("differing_field", ["agent_id", "host_id"])
def test_manifest_merge_refuses_different_concrete_agent_identity(
    differing_field: str,
    local_host: Host,
    temp_mngr_ctx: MngrContext,
) -> None:
    agent_name, agent_id = _write_agent_record_and_session(local_host, "codex", preserve_on_destroy=True)
    preserve_host_agents_on_destroy(local_host, temp_mngr_ctx, AgentTypeName("codex"), _items_when_opted_in)
    dest = get_local_preserved_agent_dir(temp_mngr_ctx, agent_name, agent_id)
    manifest_path = dest / PRESERVATION_MANIFEST_FILENAME
    original = manifest_path.read_text()
    existing = PreservationManifest.model_validate_json(original)
    different_identity = (
        existing.identity.model_copy_update(to_update(existing.identity.field_ref().agent_id, AgentId.generate()))
        if differing_field == "agent_id"
        else existing.identity.model_copy_update(to_update(existing.identity.field_ref().host_id, HostId.generate()))
    )
    incoming = PreservationManifest(identity=different_identity, items=())

    with allow_warnings(match="Refusing to merge preservation manifest"):
        write_preservation_manifest(dest, incoming)

    assert manifest_path.read_text() == original


def test_manifest_written_by_a_newer_version_still_reads_and_is_never_written_over(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
) -> None:
    agent_name, agent_id = _write_agent_record_and_session(local_host, "codex", preserve_on_destroy=True)
    preserve_host_agents_on_destroy(local_host, temp_mngr_ctx, AgentTypeName("codex"), _items_when_opted_in)
    dest = get_local_preserved_agent_dir(temp_mngr_ctx, agent_name, agent_id)
    manifest_path = dest / PRESERVATION_MANIFEST_FILENAME
    from_a_newer_version = json.loads(manifest_path.read_text())
    from_a_newer_version["schema_version"] = PRESERVATION_MANIFEST_SCHEMA_VERSION + 1
    from_a_newer_version["a_field_this_version_does_not_know"] = {"kept": True}
    manifest_path.write_text(json.dumps(from_a_newer_version))

    # The newer manifest still parses, so the archive stays discoverable...
    (archive,) = discover_preserved_agent_archives(Path(temp_mngr_ctx.config.default_host_dir))
    assert archive.agent_id == agent_id

    # ...and a preservation attempt from this version leaves it byte-for-byte intact.
    with allow_warnings(match="Refusing to write a v"):
        preserve_host_agents_on_destroy(local_host, temp_mngr_ctx, AgentTypeName("codex"), _items_when_opted_in)

    assert json.loads(manifest_path.read_text()) == from_a_newer_version


class _UnreachableHost(Host):
    """Online host whose agent discovery cannot connect (e.g. a served host key that no longer matches its pin)."""

    def discover_agents(self, timeout_seconds: float | None = None) -> list[DiscoveredAgent]:
        raise HostConnectionError(
            "Failed to connect to host: SSH host key error (Host key for 203.0.113.9 does not match.)"
        )


@pytest.mark.allow_warnings(match=r"Could not discover agents on host .* to preserve their state: .*does not match")
def test_preserve_host_agents_on_destroy_tolerates_an_unreachable_host(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """A host that cannot be reached is skipped with a warning; raising would abort the destroy."""
    pyinfra_host = local_provider._create_local_pyinfra_host()
    unreachable = _UnreachableHost(
        id=local_provider.host_id,
        host_name=HostName("unreachable"),
        connector=PyinfraConnector(pyinfra_host),
        provider_instance=local_provider,
        mngr_ctx=local_provider.mngr_ctx,
    )

    preserve_host_agents_on_destroy(unreachable, temp_mngr_ctx, AgentTypeName("codex"), _items_when_opted_in)

    assert not (Path(temp_mngr_ctx.config.default_host_dir) / "preserved").exists()


def test_preserve_host_agents_on_destroy_skips_non_readable_host(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """A host with no readable volume is a no-op (does not raise, preserves nothing)."""
    offline = OfflineHost(
        id=local_provider.host_id,
        provider_instance=local_provider,
        mngr_ctx=temp_mngr_ctx,
        certified_host_data=CertifiedHostData(
            host_id=str(local_provider.host_id),
            host_name="local",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        ),
    )
    assert not isinstance(offline, HostFileReadInterface)

    # Must return without raising even though the host exposes no readable volume.
    preserve_host_agents_on_destroy(offline, temp_mngr_ctx, AgentTypeName("codex"), _items_when_opted_in)


def test_dedupe_by_resolved_path_preserves_first_seen_order(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    other = tmp_path / "other"
    other.mkdir()

    # ``link`` resolves to ``real`` (already seen) and the trailing ``real`` is a literal
    # repeat -- both collapse, and the first-seen unresolved paths keep their order.
    assert dedupe_by_resolved_path([real, link, other, real]) == [real, other]


def test_run_adopt_session_preflight_noop_without_sessions(temp_mngr_ctx: MngrContext) -> None:
    calls: list[str] = []
    run_adopt_session_preflight(AgentTypeName("claude"), (), temp_mngr_ctx, object, calls.append)
    assert calls == []


def test_run_adopt_session_preflight_resolves_each_session_for_matching_type(temp_mngr_ctx: MngrContext) -> None:
    claude_class = resolve_agent_type(AgentTypeName("claude"), temp_mngr_ctx.config).agent_class
    calls: list[str] = []
    run_adopt_session_preflight(AgentTypeName("claude"), ("a", "b"), temp_mngr_ctx, claude_class, calls.append)
    assert calls == ["a", "b"]


def test_run_adopt_session_preflight_skips_for_nonmatching_type(temp_mngr_ctx: MngrContext) -> None:
    class _Unrelated:
        pass

    calls: list[str] = []
    run_adopt_session_preflight(AgentTypeName("claude"), ("a",), temp_mngr_ctx, _Unrelated, calls.append)
    assert calls == []


def test_require_unique_match_returns_the_lone_match() -> None:
    assert require_unique_match([Path("/a")], not_found_message="nope", ambiguous_message="many:") == Path("/a")


def test_require_unique_match_raises_not_found_for_zero() -> None:
    with pytest.raises(UserInputError, match="session xyz not found"):
        require_unique_match([], not_found_message="session xyz not found", ambiguous_message="many:")


def test_require_unique_match_raises_ambiguous_for_many_with_listing_last() -> None:
    with pytest.raises(UserInputError) as exc:
        require_unique_match(
            [Path("/a"), Path("/b")],
            not_found_message="nope",
            ambiguous_message="found in multiple stores; pass the full path:",
        )
    message = str(exc.value)
    # The single message comes first, then the candidate list last (one per indented line).
    assert message == "found in multiple stores; pass the full path:\n  /a\n  /b"


def test_adopt_sessions_copies_all_explicit_and_resumes_the_last() -> None:
    copied: list[str] = []
    resumed: list[str] = []

    def copy_explicit(arg: str) -> str:
        copied.append(arg)
        return f"id:{arg}"

    def copy_clone(_location: HostLocation) -> str:
        raise AssertionError("copy_clone must not run without a --from source")

    adopt_sessions(("a", "b", "c"), None, copy_explicit=copy_explicit, copy_clone=copy_clone, resume=resumed.append)
    # Every named session is copied; the last is the one resumed.
    assert copied == ["a", "b", "c"]
    assert resumed == ["id:c"]


def test_adopt_sessions_clone_is_resumed_over_explicit(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    assert isinstance(host, Host)
    location = HostLocation(host=host, path=tmp_path)
    copied: list[str] = []
    cloned: list[HostLocation] = []
    resumed: list[str] = []

    def copy_explicit(arg: str) -> str:
        copied.append(arg)
        return f"id:{arg}"

    def copy_clone(loc: HostLocation) -> str:
        cloned.append(loc)
        return "clone-id"

    adopt_sessions(("a", "b"), location, copy_explicit=copy_explicit, copy_clone=copy_clone, resume=resumed.append)
    # All --adopt sessions are still copied, but the clone is the one resumed.
    assert copied == ["a", "b"]
    assert cloned == [location]
    assert resumed == ["clone-id"]


def test_adopt_sessions_clone_without_session_falls_back_to_last_explicit(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    assert isinstance(host, Host)
    location = HostLocation(host=host, path=tmp_path)
    resumed: list[str] = []

    # A --from clone with no resumable session returns None (it warns, not raises); the last
    # --adopt session is then still the one resumed.
    adopt_sessions(
        ("a", "b"),
        location,
        copy_explicit=lambda arg: f"id:{arg}",
        copy_clone=lambda _loc: None,
        resume=resumed.append,
    )
    assert resumed == ["id:b"]


def test_adopt_sessions_clone_without_session_and_no_adopt_is_fresh(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    assert isinstance(host, Host)
    location = HostLocation(host=host, path=tmp_path)
    resumed: list[str] = []

    # --from with no session and no --adopt resumes nothing (fresh start).
    def fail_explicit(_arg: str) -> str:
        raise AssertionError("copy_explicit must not run")

    adopt_sessions((), location, copy_explicit=fail_explicit, copy_clone=lambda _loc: None, resume=resumed.append)
    assert resumed == []


def test_adopt_sessions_no_op_when_neither() -> None:
    def fail_explicit(_arg: str) -> str:
        raise AssertionError("copy_explicit must not run")

    def fail_clone(_location: HostLocation) -> str:
        raise AssertionError("copy_clone must not run")

    def fail_resume(_session_id: str) -> None:
        raise AssertionError("resume must not run")

    adopt_sessions((), None, copy_explicit=fail_explicit, copy_clone=fail_clone, resume=fail_resume)
