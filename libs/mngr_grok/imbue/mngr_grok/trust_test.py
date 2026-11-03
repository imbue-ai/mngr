import stat
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

import pytest
from click.testing import CliRunner
from inline_snapshot import snapshot
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.host import Host
from imbue.mngr.utils.testing import capture_loguru
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_git_command
from imbue.mngr_grok.testing import build_env_file_prefix
from imbue.mngr_grok.testing import write_agent_env_prefix
from imbue.mngr_grok.trust import GrokFolderTrustOutcome
from imbue.mngr_grok.trust import GrokFolderTrustResult
from imbue.mngr_grok.trust import GrokTrustConsent
from imbue.mngr_grok.trust import GrokTrustError
from imbue.mngr_grok.trust import GrokTrustKey
from imbue.mngr_grok.trust import TRUSTED_FOLDERS_FILENAME
from imbue.mngr_grok.trust import compute_grok_trust_key
from imbue.mngr_grok.trust import decide_grok_trust_consent
from imbue.mngr_grok.trust import ensure_grok_folder_trust
from imbue.mngr_grok.trust import is_grok_folder_trusted
from imbue.mngr_grok.trust import merge_grok_folder_grant
from imbue.mngr_grok.trust import parse_grok_trust_key_output
from imbue.mngr_grok.trust import prompt_user_to_trust_grok_folder

_STORE_PATH: Final[Path] = Path("/home/user/.grok/trusted_folders.toml")


def _make_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _toml_quote(path: Path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


# Each layout builder returns (work dir, HOME, expected key): the key grok accepts without a trust dialog.


def _build_plain_repo(root: Path) -> tuple[Path, Path, Path]:
    init_git_repo(root / "repo")
    return root / "repo", _make_dir(root / "home"), root / "repo"


def _build_repo_subdirectory(root: Path) -> tuple[Path, Path, Path]:
    init_git_repo(root / "repo", initial_commit=False)
    return _make_dir(root / "repo" / "pkg" / "sub"), _make_dir(root / "home"), root / "repo"


def _build_worktree_under_dotted_path_inside_main(root: Path) -> tuple[Path, Path, Path]:
    init_git_repo(root / "main")
    run_git_command(
        root / "main", "worktree", "add", "-q", str(root / "main" / ".mngr" / "worktrees" / "x"), "-b", "x"
    )
    return root / "main" / ".mngr" / "worktrees" / "x", _make_dir(root / "home"), root / "main"


def _build_worktree_outside_main(root: Path) -> tuple[Path, Path, Path]:
    init_git_repo(root / "main")
    _make_dir(root / "elsewhere")
    run_git_command(root / "main", "worktree", "add", "-q", str(root / "elsewhere" / "x"), "-b", "x")
    return root / "elsewhere" / "x", _make_dir(root / "home"), root / "main"


def _build_mngr_worktree_of_home_developer_repo(root: Path) -> tuple[Path, Path, Path]:
    home = root / "home"
    init_git_repo(home / "Developer" / "repo")
    run_git_command(
        home / "Developer" / "repo", "worktree", "add", "-q", str(home / ".mngr" / "worktrees" / "x"), "-b", "mngr/x"
    )
    return home / ".mngr" / "worktrees" / "x", home, home / "Developer" / "repo"


def _build_bare_repo_worktree(root: Path) -> tuple[Path, Path, Path]:
    init_git_repo(root / "src")
    run_git_command(root, "clone", "-q", "--bare", str(root / "src"), str(root / "repo.git"))
    run_git_command(root / "repo.git", "worktree", "add", "-q", str(root / "wt1"), "main")
    return root / "wt1", _make_dir(root / "home"), root / "wt1"


def _build_dot_bare_with_git_file(root: Path) -> tuple[Path, Path, Path]:
    init_git_repo(root / "src")
    _make_dir(root / "proj")
    run_git_command(root, "clone", "-q", "--bare", str(root / "src"), str(root / "proj" / ".bare"))
    (root / "proj" / ".git").write_text("gitdir: ./.bare\n")
    run_git_command(root / "proj", "worktree", "add", "-q", str(root / "proj" / "main"), "main")
    return root / "proj" / "main", _make_dir(root / "home"), root / "proj" / "main"


# The key follows grok's source (a bare main repository has no workdir), unconfirmed against the binary.
def _build_worktree_of_bare_repo_stored_as_dot_git(root: Path) -> tuple[Path, Path, Path]:
    init_git_repo(root / "src")
    run_git_command(root, "clone", "-q", "--bare", str(root / "src"), str(root / "proj" / ".git"))
    run_git_command(root / "proj", "worktree", "add", "-q", str(root / "proj" / "wt"), "main")
    return root / "proj" / "wt", _make_dir(root / "home"), root / "proj" / "wt"


def _build_work_dir_inside_repo_rooted_at_home(root: Path) -> tuple[Path, Path, Path]:
    home = root / "home"
    init_git_repo(home, initial_commit=False)
    return _make_dir(home / "proj"), home, home / "proj"


def _build_non_git_dir(root: Path) -> tuple[Path, Path, Path]:
    return _make_dir(root / "nongit" / "dir"), _make_dir(root / "home"), root / "nongit" / "dir"


def _build_symlink_to_repo_root(root: Path) -> tuple[Path, Path, Path]:
    init_git_repo(root / "real")
    (root / "link").symlink_to(root / "real")
    return root / "link", _make_dir(root / "home"), root / "real"


def _build_symlink_to_non_git_dir(root: Path) -> tuple[Path, Path, Path]:
    _make_dir(root / "realdir")
    (root / "linkdir").symlink_to(root / "realdir")
    return root / "linkdir", _make_dir(root / "home"), root / "realdir"


def _build_symlink_into_repo_subdirectory(root: Path) -> tuple[Path, Path, Path]:
    init_git_repo(root / "real")
    _make_dir(root / "real" / "pkg")
    (root / "link").symlink_to(root / "real" / "pkg")
    return root / "link", _make_dir(root / "home"), root / "real"


def _build_separate_git_dir_repo(root: Path) -> tuple[Path, Path, Path]:
    _make_dir(root / "store")
    run_git_command(root, "init", "-q", f"--separate-git-dir={root / 'store' / 'sep.git'}", str(root / "work"))
    return root / "work", _make_dir(root / "home"), root / "work"


def _build_odd_characters_dir(root: Path) -> tuple[Path, Path, Path]:
    odd_dir = _make_dir(root / '-odd "quoted" back\\slash dir')
    return odd_dir, _make_dir(root / "home"), odd_dir


@pytest.mark.parametrize(
    "build_layout",
    [
        pytest.param(_build_plain_repo, id="plain_repo"),
        pytest.param(_build_repo_subdirectory, id="subdirectory"),
        pytest.param(_build_worktree_under_dotted_path_inside_main, id="worktree_dotted_inside_main"),
        pytest.param(_build_worktree_outside_main, id="worktree_outside_main"),
        pytest.param(_build_mngr_worktree_of_home_developer_repo, id="mngr_worktree_layout"),
        pytest.param(_build_bare_repo_worktree, id="bare_repo_worktree"),
        pytest.param(_build_dot_bare_with_git_file, id="dot_bare_with_git_file"),
        pytest.param(_build_worktree_of_bare_repo_stored_as_dot_git, id="bare_repo_stored_as_dot_git"),
        pytest.param(_build_work_dir_inside_repo_rooted_at_home, id="repo_rooted_at_home"),
        pytest.param(_build_non_git_dir, id="non_git_dir"),
        pytest.param(_build_symlink_to_repo_root, id="symlink_to_repo_root"),
        pytest.param(_build_symlink_to_non_git_dir, id="symlink_to_non_git_dir"),
        pytest.param(_build_symlink_into_repo_subdirectory, id="symlink_into_repo_subdirectory"),
        pytest.param(_build_separate_git_dir_repo, id="separate_git_dir"),
        pytest.param(_build_odd_characters_dir, id="odd_characters"),
    ],
)
def test_compute_grok_trust_key_returns_the_key_grok_accepts_for_each_layout(
    build_layout: Callable[[Path], tuple[Path, Path, Path]], local_host: Host, tmp_path: Path
) -> None:
    work_dir, home, expected_key = build_layout(tmp_path / "layout")

    trust_key = compute_grok_trust_key(local_host, work_dir, write_agent_env_prefix(tmp_path, {"HOME": str(home)}))

    assert trust_key == GrokTrustKey(path=expected_key.resolve(), is_provisional=False)


def test_compute_grok_trust_key_is_provisional_for_home_that_is_a_repo_root(local_host: Host, tmp_path: Path) -> None:
    home = tmp_path / "home"
    init_git_repo(home, initial_commit=False)

    trust_key = compute_grok_trust_key(local_host, home, write_agent_env_prefix(tmp_path, {"HOME": str(home)}))

    assert trust_key == GrokTrustKey(path=home.resolve(), is_provisional=True)


def test_compute_grok_trust_key_is_provisional_for_plain_home(local_host: Host, tmp_path: Path) -> None:
    home = _make_dir(tmp_path / "home")
    home_link = tmp_path / "home_link"
    home_link.symlink_to(home)

    trust_key = compute_grok_trust_key(local_host, home, write_agent_env_prefix(tmp_path, {"HOME": str(home_link)}))

    assert trust_key == GrokTrustKey(path=home.resolve(), is_provisional=True)


def test_compute_grok_trust_key_is_provisional_for_filesystem_root(local_host: Host, tmp_path: Path) -> None:
    home = _make_dir(tmp_path / "home")

    trust_key = compute_grok_trust_key(local_host, Path("/"), write_agent_env_prefix(tmp_path, {"HOME": str(home)}))

    assert trust_key == GrokTrustKey(path=Path("/"), is_provisional=True)


def test_compute_grok_trust_key_ignores_inherited_git_environment(
    local_host: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_dir, home, expected_key = _build_repo_subdirectory(tmp_path / "layout")
    init_git_repo(tmp_path / "unrelated")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "unrelated" / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "unrelated"))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(expected_key.resolve()))

    trust_key = compute_grok_trust_key(local_host, work_dir, write_agent_env_prefix(tmp_path, {"HOME": str(home)}))

    assert trust_key.path == expected_key.resolve()


def test_compute_grok_trust_key_output_is_not_polluted_by_bash_env(
    local_host: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_dir, home, expected_key = _build_plain_repo(tmp_path / "layout")
    bash_env_path = tmp_path / "bash_env.sh"
    bash_env_path.write_text("echo polluted-by-bash-env-72914\n")
    monkeypatch.setenv("BASH_ENV", str(bash_env_path))

    trust_key = compute_grok_trust_key(local_host, work_dir, write_agent_env_prefix(tmp_path, {"HOME": str(home)}))

    assert trust_key.path == expected_key.resolve()


def test_compute_grok_trust_key_raises_for_a_missing_work_dir(local_host: Host, tmp_path: Path) -> None:
    with pytest.raises(GrokTrustError, match="does not exist on the host"):
        compute_grok_trust_key(
            local_host, tmp_path / "missing", write_agent_env_prefix(tmp_path, {"HOME": str(tmp_path)})
        )


def _write_exiting_env_prefix(tmp_path: Path) -> str:
    """An env-file prefix whose agent env file prints a line and then exits the host shell with status 3."""
    agent_env_path = tmp_path / f"agent_env_{uuid4().hex}"
    agent_env_path.write_text("echo env-file-output-38151\nexit 3\n")
    return build_env_file_prefix(tmp_path / "absent_host_env", agent_env_path)


def test_compute_grok_trust_key_raises_with_the_host_output_when_the_host_command_fails(
    local_host: Host, tmp_path: Path
) -> None:
    with pytest.raises(GrokTrustError, match=r"for .* on the host \(stdout='env-file-output-38151'"):
        compute_grok_trust_key(local_host, tmp_path, _write_exiting_env_prefix(tmp_path))


@pytest.mark.parametrize(
    "command_stdout, message",
    [
        pytest.param("/repo", "unexpected output", id="no_status_line"),
        pytest.param("/repo\ngrok-trust-key-status=3", "exited with status 3", id="unknown_status"),
        pytest.param("repo\ngrok-trust-key-status=0", "is not absolute", id="relative_key"),
        pytest.param("grok-trust-key-status=0", "is not absolute", id="empty_key"),
    ],
)
def test_parse_grok_trust_key_output_rejects_malformed_output(command_stdout: str, message: str) -> None:
    with pytest.raises(GrokTrustError, match=message):
        parse_grok_trust_key_output(command_stdout, Path("/repo"))


def test_parse_grok_trust_key_output_keeps_a_newline_inside_the_key() -> None:
    trust_key = parse_grok_trust_key_output("/odd\nname\ngrok-trust-key-status=0", Path("/odd\nname"))

    assert trust_key == GrokTrustKey(path=Path("/odd\nname"), is_provisional=False)


@pytest.mark.parametrize(
    "is_trusted_by_user_store, is_auto_dismiss_dialogs_at_startup, is_auto_approve, is_interactive, expected",
    [
        pytest.param(True, False, False, False, GrokTrustConsent.ALREADY_GRANTED, id="user_store_trusts"),
        pytest.param(True, True, True, True, GrokTrustConsent.ALREADY_GRANTED, id="user_store_trusts_with_flags"),
        pytest.param(False, True, False, False, GrokTrustConsent.PRE_APPROVED, id="auto_dismiss"),
        pytest.param(False, False, True, False, GrokTrustConsent.PRE_APPROVED, id="yes_flag"),
        pytest.param(False, True, False, True, GrokTrustConsent.PRE_APPROVED, id="opt_in_beats_prompt"),
        pytest.param(False, False, False, True, GrokTrustConsent.ASK_USER, id="interactive"),
        pytest.param(False, False, False, False, GrokTrustConsent.REFUSE, id="non_interactive_without_opt_in"),
    ],
)
def test_decide_grok_trust_consent_follows_the_consent_matrix(
    is_trusted_by_user_store: bool,
    is_auto_dismiss_dialogs_at_startup: bool,
    is_auto_approve: bool,
    is_interactive: bool,
    expected: GrokTrustConsent,
) -> None:
    consent = decide_grok_trust_consent(
        is_trusted_by_user_store=is_trusted_by_user_store,
        is_auto_dismiss_dialogs_at_startup=is_auto_dismiss_dialogs_at_startup,
        is_auto_approve=is_auto_approve,
        is_interactive=is_interactive,
    )

    assert consent == expected


@pytest.mark.parametrize(
    "store_content, is_trusted",
    [
        pytest.param("", False, id="empty"),
        pytest.param('[folders."/repo"]\ntrusted = true\ndecided_at = 1780000000\n', True, id="exact_grant"),
        pytest.param('[folders."/repo"]\ntrusted = false\n', False, id="exact_decline"),
        pytest.param('[folders."/"]\ntrusted = true\n[folders."/rep"]\ntrusted = true\n', False, id="other_keys"),
        pytest.param('[folders."/repo/sub"]\ntrusted = true\n', False, id="subdirectory_grant"),
        pytest.param('folders = { "/repo" = { trusted = true } }\n', True, id="inline_table"),
    ],
)
def test_is_grok_folder_trusted_requires_an_exact_trusted_grant(store_content: str, is_trusted: bool) -> None:
    assert is_grok_folder_trusted(store_content, Path("/repo"), _STORE_PATH) is is_trusted


@pytest.mark.parametrize(
    "store_content, message",
    [
        pytest.param('[folders\n"/repo"', "not valid TOML", id="malformed_toml"),
        pytest.param('[folders."/repo"]\ntrusted = 1\n[folders."/repo"]\ntrusted = 2\n', "not valid TOML", id="dup"),
        pytest.param("folders = 3\n", "non-table `folders`", id="folders_not_a_table"),
        pytest.param('[folders]\n"/repo" = true\n', "malformed entry", id="entry_not_a_table"),
        pytest.param('[folders."/repo"]\ntrusted = "yes"\n', "malformed entry", id="trusted_not_a_bool"),
    ],
)
def test_trust_store_readers_refuse_malformed_content(store_content: str, message: str) -> None:
    with pytest.raises(UserInputError, match=message):
        is_grok_folder_trusted(store_content, Path("/repo"), _STORE_PATH)
    with pytest.raises(UserInputError, match=message):
        merge_grok_folder_grant(store_content, Path("/repo"), _STORE_PATH)


def test_merge_grok_folder_grant_into_an_empty_store_writes_only_the_grant() -> None:
    assert merge_grok_folder_grant("", Path("/repo"), _STORE_PATH) == snapshot('[folders."/repo"]\ntrusted = true\n')


def test_merge_grok_folder_grant_leaves_an_existing_matching_grant_alone() -> None:
    store_content = '# mine\n[folders."/repo"]\ntrusted = true\ndecided_at = 1780000000\n'

    assert merge_grok_folder_grant(store_content, Path("/repo"), _STORE_PATH) is None


def test_merge_grok_folder_grant_preserves_existing_grants_comments_and_tables() -> None:
    store_content = (
        "# hand-written note\n"
        '[folders."/other"]\n'
        "trusted = true\n"
        "decided_at = 1780000000\n"
        "\n"
        '[folders."/declined"]\n'
        "trusted = false\n"
        "\n"
        "[unrelated]\n"
        "value = 1\n"
    )

    merged = merge_grok_folder_grant(store_content, Path("/repo"), _STORE_PATH)

    assert merged is not None
    assert merged.startswith("# hand-written note\n")
    assert tomllib.loads(merged) == {
        "folders": {
            "/other": {"trusted": True, "decided_at": 1780000000},
            "/declined": {"trusted": False},
            "/repo": {"trusted": True},
        },
        "unrelated": {"value": 1},
    }


def test_merge_grok_folder_grant_adds_to_an_inline_folders_table() -> None:
    merged = merge_grok_folder_grant('folders = { "/other" = { trusted = true } }\n', Path("/repo"), _STORE_PATH)

    assert merged is not None
    assert tomllib.loads(merged) == {"folders": {"/other": {"trusted": True}, "/repo": {"trusted": True}}}
    assert is_grok_folder_trusted(merged, Path("/repo"), _STORE_PATH)


def test_merge_grok_folder_grant_flips_a_recorded_decline_and_keeps_its_other_fields() -> None:
    merged = merge_grok_folder_grant(
        '[folders."/repo"]\ntrusted = false\ndecided_at = 1780000000\n', Path("/repo"), _STORE_PATH
    )

    assert merged is not None
    assert tomllib.loads(merged) == {"folders": {"/repo": {"trusted": True, "decided_at": 1780000000}}}


@pytest.mark.parametrize(
    "trust_key",
    [
        pytest.param(Path('/work/a "quoted" dir'), id="double_quotes"),
        pytest.param(Path("/work/back\\slash"), id="backslash"),
        pytest.param(Path("/work/it's \\\" both\ttab"), id="mixed"),
    ],
)
def test_merge_grok_folder_grant_round_trips_keys_that_need_toml_escaping(trust_key: Path) -> None:
    merged = merge_grok_folder_grant('[folders."/other"]\ntrusted = true\n', trust_key, _STORE_PATH)

    assert merged is not None
    assert tomllib.loads(merged)["folders"][str(trust_key)] == {"trusted": True}
    assert is_grok_folder_trusted(merged, trust_key, _STORE_PATH)


class _GrokTrustScene(FrozenModel):
    """A git work dir, the agent HOME and the store paths one ensure_grok_folder_trust call works with."""

    work_dir: Path = Field(description="The agent's work dir, a plain repository root")
    trust_key: Path = Field(description="The work dir's expected trust key")
    env_prefix: str = Field(description="Env-file prefix that sets the agent's HOME")
    agent_grok_home: Path = Field(description="The agent's per-agent grok home")
    user_store_path: Path = Field(description="The user's trusted_folders.toml")

    @property
    def agent_store_path(self) -> Path:
        return self.agent_grok_home / TRUSTED_FOLDERS_FILENAME


def _make_trust_scene(tmp_path: Path, work_dir_name: str) -> _GrokTrustScene:
    home = _make_dir(tmp_path / "home")
    work_dir = tmp_path / work_dir_name
    init_git_repo(work_dir)
    return _GrokTrustScene(
        work_dir=work_dir,
        trust_key=work_dir.resolve(),
        env_prefix=write_agent_env_prefix(tmp_path, {"HOME": str(home)}),
        agent_grok_home=tmp_path / "agents" / "agent-1" / "plugin" / "grok" / "home",
        user_store_path=home / ".grok" / TRUSTED_FOLDERS_FILENAME,
    )


def _ensure_trust(
    local_host: Host,
    scene: _GrokTrustScene,
    is_auto_dismiss_dialogs_at_startup: bool,
    is_auto_approve: bool,
    is_interactive: bool,
    prompt_user_to_trust: Callable[[Path, Path], bool],
) -> GrokFolderTrustResult:
    return ensure_grok_folder_trust(
        host=local_host,
        work_dir=scene.work_dir,
        env_prefix=scene.env_prefix,
        user_grok_home=scene.user_store_path.parent,
        agent_grok_home=scene.agent_grok_home,
        is_auto_dismiss_dialogs_at_startup=is_auto_dismiss_dialogs_at_startup,
        is_auto_approve=is_auto_approve,
        is_interactive=is_interactive,
        prompt_user_to_trust=prompt_user_to_trust,
    )


def _fail_if_prompted(trust_key: Path, user_store_path: Path) -> bool:
    raise AssertionError(f"Unexpected trust prompt for {trust_key} ({user_store_path})")


def _decline(trust_key: Path, user_store_path: Path) -> bool:
    return False


def _read_folders(store_path: Path) -> dict[str, Any]:
    return tomllib.loads(store_path.read_text())["folders"]


def test_ensure_grok_folder_trust_seeds_silently_when_the_user_store_already_trusts_the_key(
    local_host: Host, tmp_path: Path
) -> None:
    scene = _make_trust_scene(tmp_path, "project")
    user_store_content = f"# mine\n[folders.{_toml_quote(scene.trust_key)}]\ntrusted = true\ndecided_at = 1780000000\n"
    scene.user_store_path.parent.mkdir(parents=True)
    scene.user_store_path.write_text(user_store_content)

    result = _ensure_trust(local_host, scene, False, False, False, _fail_if_prompted)

    assert result.outcome == GrokFolderTrustOutcome.ALREADY_TRUSTED
    assert _read_folders(scene.agent_store_path) == {str(scene.trust_key): {"trusted": True}}
    assert scene.user_store_path.read_text() == user_store_content


@pytest.mark.parametrize(
    "is_auto_dismiss_dialogs_at_startup, is_auto_approve",
    [pytest.param(True, False, id="auto_dismiss_dialogs_at_startup"), pytest.param(False, True, id="yes_flag")],
)
def test_ensure_grok_folder_trust_grants_without_prompting_when_pre_approved(
    is_auto_dismiss_dialogs_at_startup: bool, is_auto_approve: bool, local_host: Host, tmp_path: Path
) -> None:
    scene = _make_trust_scene(tmp_path, "project")

    result = _ensure_trust(
        local_host, scene, is_auto_dismiss_dialogs_at_startup, is_auto_approve, False, _fail_if_prompted
    )

    assert result == GrokFolderTrustResult(
        trust_key=GrokTrustKey(path=scene.trust_key, is_provisional=False), outcome=GrokFolderTrustOutcome.GRANTED
    )
    assert _read_folders(scene.agent_store_path) == {str(scene.trust_key): {"trusted": True}}
    assert _read_folders(scene.user_store_path) == {str(scene.trust_key): {"trusted": True}}
    assert stat.S_IMODE(scene.agent_store_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(scene.user_store_path.stat().st_mode) == 0o600


def test_ensure_grok_folder_trust_asks_on_a_terminal_and_grants_on_yes(local_host: Host, tmp_path: Path) -> None:
    scene = _make_trust_scene(tmp_path, "project")
    prompts: list[tuple[Path, Path]] = []

    def accept(trust_key: Path, user_store_path: Path) -> bool:
        prompts.append((trust_key, user_store_path))
        return True

    result = _ensure_trust(local_host, scene, False, False, True, accept)

    assert result.outcome == GrokFolderTrustOutcome.GRANTED
    assert prompts == [(scene.trust_key, scene.user_store_path)]
    assert _read_folders(scene.agent_store_path) == {str(scene.trust_key): {"trusted": True}}
    assert _read_folders(scene.user_store_path) == {str(scene.trust_key): {"trusted": True}}


def test_ensure_grok_folder_trust_keeps_a_grant_recorded_while_the_user_was_asked(
    local_host: Host, tmp_path: Path
) -> None:
    scene = _make_trust_scene(tmp_path, "project")
    scene.user_store_path.parent.mkdir(parents=True)
    scene.user_store_path.write_text('[folders."/first"]\ntrusted = true\n')

    def accept_after_another_grant_lands(trust_key: Path, user_store_path: Path) -> bool:
        user_store_path.write_text('[folders."/first"]\ntrusted = true\n\n[folders."/meanwhile"]\ntrusted = true\n')
        return True

    _ensure_trust(local_host, scene, False, False, True, accept_after_another_grant_lands)

    assert _read_folders(scene.user_store_path) == {
        "/first": {"trusted": True},
        "/meanwhile": {"trusted": True},
        str(scene.trust_key): {"trusted": True},
    }


def test_ensure_grok_folder_trust_exits_and_writes_nothing_when_the_user_declines(
    local_host: Host, tmp_path: Path
) -> None:
    scene = _make_trust_scene(tmp_path, "project")

    with capture_loguru(level="ERROR") as log_output:
        with pytest.raises(SystemExit) as exit_info:
            _ensure_trust(local_host, scene, False, False, True, _decline)

    assert exit_info.value.code == 1
    assert "declined to trust" in log_output.getvalue()
    assert not scene.agent_store_path.exists()
    assert not scene.user_store_path.exists()


def test_ensure_grok_folder_trust_exits_naming_both_opt_ins_when_no_terminal_and_no_opt_in(
    local_host: Host, tmp_path: Path
) -> None:
    scene = _make_trust_scene(tmp_path, "project")

    with capture_loguru(level="ERROR") as log_output:
        with pytest.raises(SystemExit) as exit_info:
            _ensure_trust(local_host, scene, False, False, False, _fail_if_prompted)

    assert exit_info.value.code == 1
    assert "`--yes`" in log_output.getvalue()
    assert "`auto_dismiss_dialogs_at_startup = true`" in log_output.getvalue()
    assert not scene.agent_store_path.exists()
    assert not scene.user_store_path.exists()


def test_ensure_grok_folder_trust_seeds_nothing_for_a_provisional_key(local_host: Host, tmp_path: Path) -> None:
    home = _make_dir(tmp_path / "home")
    agent_grok_home = tmp_path / "agent_grok_home"

    result = ensure_grok_folder_trust(
        host=local_host,
        work_dir=home,
        env_prefix=write_agent_env_prefix(tmp_path, {"HOME": str(home)}),
        user_grok_home=home / ".grok",
        agent_grok_home=agent_grok_home,
        is_auto_dismiss_dialogs_at_startup=False,
        is_auto_approve=False,
        is_interactive=False,
        prompt_user_to_trust=_fail_if_prompted,
    )

    assert result.outcome == GrokFolderTrustOutcome.PROVISIONAL
    assert result.trust_key == GrokTrustKey(path=home.resolve(), is_provisional=True)
    assert not (agent_grok_home / TRUSTED_FOLDERS_FILENAME).exists()
    assert not (home / ".grok").exists()


def test_ensure_grok_folder_trust_never_rewrites_a_matching_grant_in_the_agent_store(
    local_host: Host, tmp_path: Path
) -> None:
    scene = _make_trust_scene(tmp_path, "project")
    agent_store_content = f"# seeded earlier\n[folders.{_toml_quote(scene.trust_key)}]\ntrusted = true\n"
    scene.agent_store_path.parent.mkdir(parents=True)
    scene.agent_store_path.write_text(agent_store_content)
    inode_before = scene.agent_store_path.stat().st_ino

    result = _ensure_trust(local_host, scene, False, True, False, _fail_if_prompted)

    assert result.outcome == GrokFolderTrustOutcome.GRANTED
    assert scene.agent_store_path.read_text() == agent_store_content
    assert scene.agent_store_path.stat().st_ino == inode_before


def test_ensure_grok_folder_trust_merges_into_the_user_store_in_place(local_host: Host, tmp_path: Path) -> None:
    scene = _make_trust_scene(tmp_path, "project")
    dotfiles_store = tmp_path / "dotfiles" / TRUSTED_FOLDERS_FILENAME
    dotfiles_store.parent.mkdir()
    dotfiles_store.write_text('# my grants\n[folders."/elsewhere"]\ntrusted = true\ndecided_at = 1780000000\n')
    dotfiles_store.chmod(0o644)
    scene.user_store_path.parent.mkdir(parents=True)
    scene.user_store_path.symlink_to(dotfiles_store)

    _ensure_trust(local_host, scene, False, True, False, _fail_if_prompted)

    assert scene.user_store_path.is_symlink()
    assert dotfiles_store.read_text().startswith("# my grants\n")
    assert _read_folders(dotfiles_store) == {
        "/elsewhere": {"trusted": True, "decided_at": 1780000000},
        str(scene.trust_key): {"trusted": True},
    }
    assert stat.S_IMODE(dotfiles_store.stat().st_mode) == 0o644


def test_ensure_grok_folder_trust_records_a_key_with_quotes_and_backslashes(local_host: Host, tmp_path: Path) -> None:
    scene = _make_trust_scene(tmp_path, 'pro"ject\\ name')

    _ensure_trust(local_host, scene, True, False, False, _fail_if_prompted)

    assert '"' in str(scene.trust_key) and "\\" in str(scene.trust_key)
    assert _read_folders(scene.agent_store_path) == {str(scene.trust_key): {"trusted": True}}
    assert _read_folders(scene.user_store_path) == {str(scene.trust_key): {"trusted": True}}


def test_ensure_grok_folder_trust_refuses_a_malformed_user_store_before_writing(
    local_host: Host, tmp_path: Path
) -> None:
    scene = _make_trust_scene(tmp_path, "project")
    scene.user_store_path.parent.mkdir(parents=True)
    scene.user_store_path.write_text("[folders\nnot toml")

    with pytest.raises(UserInputError, match="not valid TOML"):
        _ensure_trust(local_host, scene, True, False, False, _fail_if_prompted)

    assert scene.user_store_path.read_text() == "[folders\nnot toml"
    assert not scene.agent_store_path.exists()


def test_prompt_user_to_trust_grok_folder_defaults_to_no_and_names_what_trust_enables(tmp_path: Path) -> None:
    with capture_loguru(level="INFO") as log_output:
        with CliRunner().isolation(input="\n") as (prompt_output, _, _):
            is_trusted = prompt_user_to_trust_grok_folder(Path("/repo"), tmp_path / TRUSTED_FOLDERS_FILENAME)
            prompt_text = prompt_output.getvalue().decode()

    assert is_trusted is False
    assert "Trust /repo for grok? [y/N]" in prompt_text
    for surface in ("MCP servers", "LSP config", ".envrc", "project hooks", "plugins"):
        assert surface in log_output.getvalue()


def test_prompt_user_to_trust_grok_folder_accepts_yes() -> None:
    with CliRunner().isolation(input="y\n"):
        is_trusted = prompt_user_to_trust_grok_folder(Path("/repo"), Path("/home/user/.grok/trusted_folders.toml"))

    assert is_trusted is True
