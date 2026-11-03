import subprocess
import uuid
from collections.abc import Mapping
from pathlib import Path

import pytest

from imbue.mngr.api.providers import get_local_host
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.primitives import AgentId
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.testing import capture_loguru
from imbue.mngr.utils.testing import make_local_host_of_class
from imbue.mngr_grok.grok_config import FORK_FROM_RELATIVE_PATH
from imbue.mngr_grok.grok_config import SESSION_ID_RELATIVE_PATH
from imbue.mngr_grok.grok_config import SessionBucketNameError
from imbue.mngr_grok.grok_config import compute_session_bucket_name
from imbue.mngr_grok.grok_sessions import GROK_SESSIONS_RELATIVE_PATH
from imbue.mngr_grok.grok_sessions import GrokSessionError
from imbue.mngr_grok.grok_sessions import GrokSessionSource
from imbue.mngr_grok.grok_sessions import SESSION_SUMMARY_FILENAME
from imbue.mngr_grok.grok_sessions import build_destination_session_dir
from imbue.mngr_grok.grok_sessions import build_grok_preserved_items
from imbue.mngr_grok.grok_sessions import build_resumable_session_dirs_command
from imbue.mngr_grok.grok_sessions import copy_adopted_grok_session
from imbue.mngr_grok.grok_sessions import copy_cloned_grok_session
from imbue.mngr_grok.grok_sessions import find_resumable_session_dir
from imbue.mngr_grok.grok_sessions import get_local_user_grok_home
from imbue.mngr_grok.grok_sessions import is_valid_grok_session_id
from imbue.mngr_grok.grok_sessions import parse_resumable_session_dirs
from imbue.mngr_grok.grok_sessions import read_recorded_grok_session_id
from imbue.mngr_grok.grok_sessions import record_grok_fork_source
from imbue.mngr_grok.grok_sessions import resolve_canonical_work_dir
from imbue.mngr_grok.grok_sessions import resolve_grok_adopt_session
from imbue.mngr_grok.testing import make_grok_session_dir


def test_preserved_items_cover_transcripts_sessions_and_session_pointers() -> None:
    items = build_grok_preserved_items(is_memory_enabled=False)

    assert [(item.rel_path, item.kind) for item in items] == [
        ("logs/grok_transcript", FileType.DIRECTORY),
        ("events/grok/common_transcript", FileType.DIRECTORY),
        ("plugin/grok/home/sessions", FileType.DIRECTORY),
        ("plugin/grok/session_id", FileType.FILE),
        ("plugin/grok/roots", FileType.FILE),
    ]


def test_preserved_items_add_the_memory_store_when_memory_is_on() -> None:
    items = build_grok_preserved_items(is_memory_enabled=True)

    assert items[-1].rel_path == "plugin/grok/home/memory-v2"
    assert items[-1].kind == FileType.DIRECTORY
    assert len(items) == len(build_grok_preserved_items(is_memory_enabled=False)) + 1


@pytest.mark.parametrize(
    ("candidate", "is_valid"),
    [
        ("01a156d6-f927-7091-91a4-f6692a5800da", True),
        ("session.v2_x", True),
        ("", False),
        ("../escape", False),
        ("a/b", False),
        (".hidden", False),
        ("a" * 129, False),
    ],
)
def test_is_valid_grok_session_id(candidate: str, is_valid: bool) -> None:
    assert is_valid_grok_session_id(candidate) is is_valid


@pytest.mark.parametrize("shell", ["bash", "sh"])
def test_resumable_session_dirs_command_lists_each_bucket_holding_a_summary(tmp_path: Path, shell: str) -> None:
    sessions_dir = tmp_path / "sessions with space"
    session_id = str(uuid.uuid4())
    first = make_grok_session_dir(sessions_dir, "%2Fa", session_id)
    second = make_grok_session_dir(sessions_dir, "%2Fb", session_id)
    make_grok_session_dir(sessions_dir, "%2Fc", session_id, is_resumable=False)
    make_grok_session_dir(sessions_dir, "%2Fa", str(uuid.uuid4()))

    result = subprocess.run(
        [shell, "-c", build_resumable_session_dirs_command(sessions_dir, session_id)],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )

    assert parse_resumable_session_dirs(result.stdout) == [first, second]


def test_resumable_session_dirs_command_prints_nothing_without_a_sessions_dir(tmp_path: Path) -> None:
    result = subprocess.run(
        ["sh", "-c", build_resumable_session_dirs_command(tmp_path / "missing", "abc")],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )

    assert parse_resumable_session_dirs(result.stdout) == []


def test_find_resumable_session_dir_skips_a_copy_without_summary(local_host: Host, tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    session_id = str(uuid.uuid4())
    make_grok_session_dir(sessions_dir, "%2Fw", session_id, is_resumable=False)

    assert find_resumable_session_dir(local_host, sessions_dir, session_id) is None
    resumable_dir = make_grok_session_dir(sessions_dir, "%2Fv", session_id)
    assert find_resumable_session_dir(local_host, sessions_dir, session_id) == resumable_dir


class _FailingCommandHost(Host):
    """The local host, except that every idempotent command fails as it would over a lost connection."""

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        raise_on_timeout: bool = False,
    ) -> CommandResult:
        return CommandResult(stdout="", stderr="connection lost", success=False)


def test_find_resumable_session_dir_raises_when_the_host_command_fails(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    failing_host = make_local_host_of_class(local_provider, _FailingCommandHost)

    with pytest.raises(GrokSessionError, match=r"Could not search .* \(stderr='connection lost'\)"):
        find_resumable_session_dir(failing_host, tmp_path / "sessions", "sid-1")


@pytest.mark.parametrize(("content", "expected"), [(None, None), ("", None), ("  \n", None), ("sid-1\n", "sid-1")])
def test_read_recorded_grok_session_id(
    local_host: Host, tmp_path: Path, content: str | None, expected: str | None
) -> None:
    if content is not None:
        session_id_path = tmp_path / SESSION_ID_RELATIVE_PATH
        session_id_path.parent.mkdir(parents=True)
        session_id_path.write_text(content)

    assert read_recorded_grok_session_id(local_host, tmp_path) == expected


def test_resolve_canonical_work_dir_follows_symlinks(local_host: Host, tmp_path: Path) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real_dir)

    assert resolve_canonical_work_dir(local_host, link) == str(real_dir.resolve())


def test_resolve_canonical_work_dir_raises_for_a_missing_dir(local_host: Host, tmp_path: Path) -> None:
    with pytest.raises(GrokSessionError, match="canonical path"):
        resolve_canonical_work_dir(local_host, tmp_path / "missing")


def test_build_destination_session_dir_uses_the_work_dir_bucket(tmp_path: Path) -> None:
    session_dir = build_destination_session_dir(tmp_path, "/home/u/repo", "sid")

    assert session_dir == tmp_path / "plugin/grok/home/sessions/%2Fhome%2Fu%2Frepo/sid"


def test_build_destination_session_dir_refuses_a_bucket_grok_would_hash(tmp_path: Path) -> None:
    with pytest.raises(SessionBucketNameError, match="too long"):
        build_destination_session_dir(tmp_path, "/" + "d" * 300, "sid")


@pytest.mark.rsync
def test_copy_cloned_grok_session_copies_the_recorded_session_into_the_new_bucket(
    local_host: Host, tmp_path: Path
) -> None:
    source_state_dir = tmp_path / "source"
    session_id = str(uuid.uuid4())
    make_grok_session_dir(source_state_dir / GROK_SESSIONS_RELATIVE_PATH, "%2Fold%2Fwork", session_id)
    (source_state_dir / SESSION_ID_RELATIVE_PATH).write_text(session_id + "\n")
    make_grok_session_dir(source_state_dir / GROK_SESSIONS_RELATIVE_PATH, "%2Fold%2Fwork", str(uuid.uuid4()))
    dest_state_dir = tmp_path / "dest"

    copied_id = copy_cloned_grok_session(
        local_host, dest_state_dir, "/new/work", HostLocation(host=local_host, path=source_state_dir)
    )

    assert copied_id == session_id
    dest_bucket = dest_state_dir / GROK_SESSIONS_RELATIVE_PATH / compute_session_bucket_name("/new/work")
    assert sorted(path.name for path in dest_bucket.iterdir()) == [session_id]
    assert (dest_bucket / session_id / SESSION_SUMMARY_FILENAME).is_file()
    assert (dest_bucket / session_id / "updates.jsonl").read_text() == '{"method": "session/update"}\n'


@pytest.mark.parametrize(
    ("recorded_session_id", "expected_warning"),
    [
        (None, "records no grok session to resume"),
        ("unresumable", "has no resumable copy of grok session unresumable"),
        ("../escaped", "records '../escaped', which is not a grok session id"),
    ],
)
def test_copy_cloned_grok_session_returns_none_without_a_resumable_session(
    local_host: Host, tmp_path: Path, recorded_session_id: str | None, expected_warning: str
) -> None:
    source_state_dir = tmp_path / "source"
    sessions_dir = source_state_dir / GROK_SESSIONS_RELATIVE_PATH
    make_grok_session_dir(sessions_dir, "%2Fw", "unresumable", is_resumable=False)
    make_grok_session_dir(sessions_dir, "", "escaped")
    if recorded_session_id is not None:
        (source_state_dir / SESSION_ID_RELATIVE_PATH).write_text(recorded_session_id + "\n")
    dest_state_dir = tmp_path / "dest"

    source_location = HostLocation(host=local_host, path=source_state_dir)
    with capture_loguru() as log_output:
        copied_id = copy_cloned_grok_session(local_host, dest_state_dir, "/w", source_location)

    assert copied_id is None
    assert expected_warning in log_output.getvalue()
    assert not dest_state_dir.exists()


@pytest.mark.rsync
def test_copy_adopted_grok_session_copies_from_the_local_machine(
    local_host: Host, temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    session_id = str(uuid.uuid4())
    source_dir = make_grok_session_dir(tmp_path / "user-sessions", "%2Fsomewhere", session_id)

    copied_id = copy_adopted_grok_session(
        local_host,
        get_local_host(temp_mngr_ctx),
        tmp_path / "dest",
        "/w",
        GrokSessionSource(session_id=session_id, session_dir=source_dir),
    )

    assert copied_id == session_id
    assert (build_destination_session_dir(tmp_path / "dest", "/w", session_id) / SESSION_SUMMARY_FILENAME).is_file()


def test_record_grok_fork_source_writes_fork_from(local_host: Host, tmp_path: Path) -> None:
    record_grok_fork_source(local_host, tmp_path, "sid-7")

    assert (tmp_path / FORK_FROM_RELATIVE_PATH).read_text() == "sid-7\n"


def test_get_local_user_grok_home_defaults_to_dot_grok(tmp_path: Path) -> None:
    assert get_local_user_grok_home() == tmp_path / ".grok"


def test_get_local_user_grok_home_honours_grok_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "custom"))

    assert get_local_user_grok_home() == tmp_path / "custom"


def test_get_local_user_grok_home_ignores_grok_home_inside_a_grok_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "parent-agent-home"))
    monkeypatch.setenv("GROK_AGENT", "1")

    assert get_local_user_grok_home() == tmp_path / ".grok"


def test_resolve_grok_adopt_session_finds_an_id_in_the_user_store(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    session_id = str(uuid.uuid4())
    session_dir = make_grok_session_dir(tmp_path / ".grok" / "sessions", "%2Fproj", session_id)

    source = resolve_grok_adopt_session(session_id, temp_mngr_ctx, tmp_path / ".grok")

    assert source == GrokSessionSource(session_id=session_id, session_dir=session_dir)


def test_resolve_grok_adopt_session_finds_an_id_in_a_live_agent_store(
    temp_mngr_ctx: MngrContext, temp_host_dir: Path, tmp_path: Path
) -> None:
    session_id = str(uuid.uuid4())
    agent_sessions_dir = temp_host_dir / "agents" / str(AgentId.generate()) / GROK_SESSIONS_RELATIVE_PATH
    session_dir = make_grok_session_dir(agent_sessions_dir, "%2Fproj", session_id)

    source = resolve_grok_adopt_session(session_id, temp_mngr_ctx, tmp_path / ".grok")

    assert source.session_dir == session_dir


def test_resolve_grok_adopt_session_rejects_an_id_found_twice(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    session_id = str(uuid.uuid4())
    make_grok_session_dir(tmp_path / ".grok" / "sessions", "%2Fa", session_id)
    make_grok_session_dir(tmp_path / ".grok" / "sessions", "%2Fb", session_id)

    with pytest.raises(UserInputError, match="more than one place"):
        resolve_grok_adopt_session(session_id, temp_mngr_ctx, tmp_path / ".grok")


@pytest.mark.parametrize("is_staged_without_summary", [False, True], ids=["no_directory", "directory_without_summary"])
def test_resolve_grok_adopt_session_rejects_an_id_without_a_resumable_directory(
    temp_mngr_ctx: MngrContext, tmp_path: Path, is_staged_without_summary: bool
) -> None:
    session_id = str(uuid.uuid4())
    make_grok_session_dir(tmp_path / ".grok" / "sessions", "%2Fa", str(uuid.uuid4()))
    if is_staged_without_summary:
        make_grok_session_dir(tmp_path / ".grok" / "sessions", "%2Fb", session_id, is_resumable=False)

    with pytest.raises(UserInputError, match="not found"):
        resolve_grok_adopt_session(session_id, temp_mngr_ctx, tmp_path / ".grok")


def test_resolve_grok_adopt_session_accepts_an_absolute_session_dir(
    temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    session_id = str(uuid.uuid4())
    session_dir = make_grok_session_dir(tmp_path / "elsewhere", "%2Fa", session_id)

    source = resolve_grok_adopt_session(str(session_dir), temp_mngr_ctx, tmp_path / ".grok")

    assert source == GrokSessionSource(session_id=session_id, session_dir=session_dir.resolve())


@pytest.mark.parametrize(
    ("session_dir_name", "is_resumable"),
    [
        pytest.param(str(uuid.uuid4()), False, id="without_summary"),
        pytest.param(".hidden", True, id="not_a_session_id"),
    ],
)
def test_resolve_grok_adopt_session_rejects_an_absolute_dir_that_is_not_a_resumable_session(
    temp_mngr_ctx: MngrContext, tmp_path: Path, session_dir_name: str, is_resumable: bool
) -> None:
    session_dir = make_grok_session_dir(tmp_path / "elsewhere", "%2Fa", session_dir_name, is_resumable=is_resumable)

    with pytest.raises(UserInputError, match="not a resumable grok session"):
        resolve_grok_adopt_session(str(session_dir), temp_mngr_ctx, tmp_path / ".grok")


def test_resolve_grok_adopt_session_rejects_an_id_that_is_not_a_file_name(
    temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    with pytest.raises(UserInputError, match="not a grok session id"):
        resolve_grok_adopt_session("../x", temp_mngr_ctx, tmp_path / ".grok")
