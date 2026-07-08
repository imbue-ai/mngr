import json
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mngr.api.preservation import PreservedItem
from imbue.mngr.api.preservation import adopt_sessions
from imbue.mngr.api.preservation import build_transcript_preserved_items
from imbue.mngr.api.preservation import dedupe_by_resolved_path
from imbue.mngr.api.preservation import flag_gated_items
from imbue.mngr.api.preservation import get_local_preserved_agent_dir
from imbue.mngr.api.preservation import get_preserved_agent_dir
from imbue.mngr.api.preservation import preserve_agent_data
from imbue.mngr.api.preservation import preserve_host_agents_on_destroy
from imbue.mngr.api.preservation import require_unique_match
from imbue.mngr.api.preservation import run_adopt_session_preflight
from imbue.mngr.config.agent_config_registry import resolve_agent_type
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.host import get_agent_state_dir_path
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.hosts.offline_host import OfflineHostWithVolume
from imbue.mngr.hosts.offline_host import make_readable_offline_host
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.data_types import VolumeFile
from imbue.mngr.interfaces.host import HostFileReadInterface
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance


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

    preserve_agent_data(_claude_like_items(), local_host, state_dir, dest_root, temp_mngr_ctx)

    _assert_mirrored(dest_root)


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
    preserve_agent_data(items, reader, state_dir, dest_root, temp_mngr_ctx)

    # The items on either side of the failure are still preserved.
    assert (dest_root / "good_before").read_text() == "before\n"
    assert (dest_root / "good_after").read_text() == "after\n"
    # The failing item produced nothing.
    assert not (dest_root / "bad").exists()


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
    # ...the codex agent that opted out is skipped...
    assert not preserved_out.exists()
    # ...and the claude agent is skipped by the type filter despite opting in.
    assert not preserved_other.exists()


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
