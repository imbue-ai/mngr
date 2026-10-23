import base64
import json
import os
import re
import subprocess
import tomllib
from collections.abc import Mapping
from collections.abc import Sequence
from importlib import metadata as importlib_metadata
from pathlib import Path
from types import ModuleType
from typing import Any
from typing import Final

import httpx
import pytest
from loguru import logger
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.bootstrap import CI_SNAPSHOT_MINDS_ROOT_NAME
from imbue.minds.bootstrap import mngr_host_dir_for
from imbue.minds.desktop_client.workspace_program_command import build_workspace_program_command
from imbue.minds.utils.snapshot_container_states_testing import DockerContainerState
from imbue.mngr_vps.container_setup import LABEL_HOST_ID
from imbue.mngr_vps.container_setup import container_tmp_tmpfs_size_mib
from imbue.mngr_vps.host_setup import expected_container_memory_cap_bytes

DEFAULT_WORKSPACE_TEMPLATE_OWNER_REPO: Final[str] = "imbue-ai/default-workspace-template"


def _github_api_headers() -> dict[str, str]:
    """The contents-API headers, authenticated when the shell carries a GitHub token.

    The repo is public, so the token adds no access; it lifts the request out of
    the anonymous per-IP rate limit that GitHub-hosted runners share, which is
    what makes an unauthenticated fetch fail there.
    """
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "mngr-template-alignment-test"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return {**headers, "Authorization": f"Bearer {token}"}
    return headers


def fetch_default_workspace_template_file(repo_relative_path: str) -> str | None:
    """Fetch a file from default-workspace-template's default branch via the GitHub contents API.

    Returns None on any fetch or decode failure, so the caller can surface a
    single "could not fetch" assertion.
    """
    url = f"https://api.github.com/repos/{DEFAULT_WORKSPACE_TEMPLATE_OWNER_REPO}/contents/{repo_relative_path}"
    try:
        response = httpx.get(url, headers=_github_api_headers(), timeout=30.0, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("Failed to fetch {}: {}", url, e)
        return None
    try:
        payload = response.json()
    except ValueError as e:
        logger.trace("JSON parse of {} failed: {}", url, e)
        return None
    # GitHub answers a directory path with a list, and an odd error path with a non-object body.
    if not isinstance(payload, dict):
        return None
    content_b64 = payload.get("content")
    if not isinstance(content_b64, str):
        return None
    try:
        return base64.b64decode(content_b64).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as e:
        logger.trace("base64/utf-8 decode of {} failed: {}", url, e)
        return None


# The mngr profile id ``stub_mngr_host_dir`` activates.
_STUB_MNGR_PROFILE_ID: Final[str] = "testprofile"

_GIT_TEST_ENV_KEYS: Final[dict[str, str]] = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@test",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@test",
}


def _git_test_env(tmp_path: Path) -> dict[str, str]:
    """Build an environment dict for git commands in tests.

    Uses deterministic author/committer info and a minimal PATH so that
    git operations are reproducible and don't depend on the user's config.
    """
    return {
        **_GIT_TEST_ENV_KEYS,
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }


def init_and_commit_git_repo(repo_dir: Path, tmp_path: Path, allow_empty: bool = False) -> None:
    """Initialize a git repo and commit all files in repo_dir.

    If allow_empty is True, creates an empty commit even when there are no
    staged files. Otherwise, all files in the directory are staged and committed.
    """
    cg = ConcurrencyGroup(name="test-git-init")
    with cg:
        cg.run_process_to_completion(command=["git", "init"], cwd=repo_dir)
        cg.run_process_to_completion(command=["git", "add", "."], cwd=repo_dir)

        commit_cmd = ["git", "commit", "-m", "init"]
        if allow_empty:
            commit_cmd.append("--allow-empty")

        cg.run_process_to_completion(
            command=commit_cmd,
            cwd=repo_dir,
            env=_git_test_env(tmp_path),
        )


def make_git_repo(tmp_path: Path, name: str = "repo") -> Path:
    """Create a minimal git repo with a committed file.

    Shared helper for tests that need a local git repo to operate on.
    Creates a directory under tmp_path with a single ``hello.txt`` file,
    initializes a git repo, and commits the file.
    """
    repo = tmp_path / name
    repo.mkdir()
    (repo / "hello.txt").write_text("hello")
    init_and_commit_git_repo(repo, tmp_path)
    return repo


def add_and_commit_git_repo(repo_dir: Path, tmp_path: Path, message: str = "update") -> None:
    """Stage all changes and commit in an existing git repo.

    Unlike init_and_commit_git_repo, this does not run ``git init`` and is
    intended for adding follow-up commits to an already-initialized repo.
    """
    cg = ConcurrencyGroup(name="test-git-commit")
    with cg:
        cg.run_process_to_completion(command=["git", "add", "."], cwd=repo_dir)
        cg.run_process_to_completion(
            command=["git", "commit", "-m", message],
            cwd=repo_dir,
            env=_git_test_env(tmp_path),
        )


def stub_mngr_host_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, root_name: str) -> Path:
    """Redirect ``Path.home()`` to ``tmp_path`` and seed a minimal mngr profile.

    Returns the active ``settings.toml`` path (the file itself may not exist
    on return -- callers populate it as needed). The bootstrap helpers refuse
    to write anything until ``config.toml`` and the matching profile dir
    exist, so we materialize them up front. ``Path.home()`` consults ``$HOME``
    on Linux/macOS, so swapping that in via ``monkeypatch.setenv`` is enough
    to redirect the helpers without touching ``Path`` itself.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    mngr_host_dir = mngr_host_dir_for(root_name)
    mngr_host_dir.mkdir(parents=True, exist_ok=True)
    (mngr_host_dir / "config.toml").write_text(f'profile = "{_STUB_MNGR_PROFILE_ID}"\n')
    settings_dir = mngr_host_dir / "profiles" / _STUB_MNGR_PROFILE_ID
    settings_dir.mkdir(parents=True, exist_ok=True)
    return settings_dir / "settings.toml"


def write_imbue_cloud_accounts_index(root_name: str, entries: Sequence[object]) -> Path:
    """Write the imbue_cloud plugin's ``accounts.json`` (its signed-in session index) under the profile
    ``stub_mngr_host_dir`` activated, with ``entries`` verbatim; returns the index path."""
    sessions_dir = (
        mngr_host_dir_for(root_name) / "profiles" / _STUB_MNGR_PROFILE_ID / "providers" / "imbue_cloud" / "sessions"
    )
    sessions_dir.mkdir(parents=True, exist_ok=True)
    accounts_path = sessions_dir / "accounts.json"
    accounts_path.write_text(json.dumps({"entries": list(entries)}))
    return accounts_path


def extract_response(exec_result: subprocess.CompletedProcess[str]) -> str:
    """Extract the model response from mngr exec output.

    Filters out mngr's "Command succeeded/failed" status lines,
    returning only the first line of actual model output.
    """
    response_lines = [
        line for line in exec_result.stdout.strip().splitlines() if line and not line.startswith("Command ")
    ]
    if not response_lines:
        raise AssertionError(f"No response from model: {exec_result.stdout!r}")
    return response_lines[0]


_BACKUP_TEST_GIT_IDENTITY: Final[tuple[str, ...]] = (
    "-c",
    "user.name=test",
    "-c",
    "user.email=test@example.com",
)


def run_git_for_backup_test(repo: Path, *args: str, env_overrides: Mapping[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *_BACKUP_TEST_GIT_IDENTITY, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        env={**os.environ, **env_overrides} if env_overrides is not None else None,
    )
    return result.stdout


def write_stub_supervisorctl(
    stub_bin: Path,
    *,
    is_restart_ok: bool = True,
    call_log_path: Path | None = None,
    status_lines: Sequence[str] | None = None,
    status_lines_after_restart: Sequence[str] | None = None,
    is_reread_ok: bool = True,
    manual_programs: Sequence[str] = (),
) -> Path:
    """Write a stub ``supervisorctl`` into ``stub_bin`` and return its path.

    ``status`` prints the roster in ``status_lines`` (defaulting to a healthy
    ``host-backup`` + ``system_interface``, with ``host-backup`` STOPPED when
    ``is_restart_ok=False``); ``status <name>`` filters it like the real tool.
    Once a ``restart`` or ``reload`` has been attempted, ``status`` switches to
    ``status_lines_after_restart`` when given -- so tests can model a machine
    whose service states differ before and after the restore's restart.
    ``avail`` lists the programs of the current roster, those named in
    ``manual_programs`` as autostart=false ("manual") and the rest as "auto".

    The ``is_restart_ok=False`` variant additionally fails ``restart`` and
    ``reload``, for exercising the update script's rollback path and the
    restore's resume-exit-code independence. ``is_reread_ok=False`` fails
    ``reread`` the way supervisorctl does on a config it cannot parse.

    ``call_log_path`` appends each invocation's arguments as one line, so tests
    can assert on service-lifecycle ordering (e.g. ``stop all`` before
    ``reload``).
    """
    if status_lines is None:
        status_lines = (
            ("host-backup RUNNING pid 123, uptime 0:00:01" if is_restart_ok else "host-backup STOPPED"),
            "system_interface RUNNING pid 124, uptime 0:00:01",
        )
    if status_lines_after_restart is None:
        status_lines_after_restart = status_lines
    restart_marker = stub_bin / ".supervisorctl-restarted"

    def _printf(lines: Sequence[str]) -> str:
        return "printf '%s\\n' " + " ".join(f"'{line}'" for line in lines)

    stub = stub_bin / "supervisorctl"
    lines = ["#!/bin/bash"]
    if call_log_path is not None:
        lines.append(f'echo "$@" >> "{call_log_path}"')
    lines.append(f'if [ "$1" = "restart" ] || [ "$1" = "reload" ]; then touch "{restart_marker}"; fi')
    if not is_restart_ok:
        lines.append('if [ "$1" = "restart" ] || [ "$1" = "reload" ]; then echo "failed" >&2; exit 1; fi')
    if is_reread_ok:
        lines.append('if [ "$1" = "reread" ]; then echo "No config updates to processes"; exit 0; fi')
    else:
        lines.append(
            'if [ "$1" = "reread" ]; then echo "ERROR: CANT_REREAD: File contains no section headers."; exit 1; fi'
        )
    if manual_programs:
        manual_pattern = "|".join(manual_programs)
        avail_mode = f'case "$name" in {manual_pattern}) mode=manual ;; *) mode=auto ;; esac; '
    else:
        avail_mode = "mode=auto; "
    lines += [
        'if [ "$1" = "avail" ]; then',
        f'    if [ -e "{restart_marker}" ]; then',
        f"        roster=$({_printf(status_lines_after_restart)})",
        "    else",
        f"        roster=$({_printf(status_lines)})",
        "    fi",
        "    printf '%s\\n' \"$roster\" | while read -r name _; do",
        f'        {avail_mode}printf \'%-32s in use    %-9s 999:999\\n\' "$name" "$mode"',
        "    done",
        "    exit 0",
        "fi",
    ]
    lines += [
        'if [ "$1" = "status" ]; then',
        f'    if [ -e "{restart_marker}" ]; then',
        f"        roster=$({_printf(status_lines_after_restart)})",
        "    else",
        f"        roster=$({_printf(status_lines)})",
        "    fi",
        '    if [ -n "$2" ]; then',
        '        printf \'%s\\n\' "$roster" | grep -E "^$2 " || true',
        "    else",
        "        printf '%s\\n' \"$roster\"",
        "    fi",
        "    exit 0",
        "fi",
    ]
    if is_restart_ok:
        lines.append('echo "host-backup RUNNING pid 123, uptime 0:00:01"')
    else:
        lines.append('echo "host-backup STOPPED"')
    lines.append("exit 0")
    stub.write_text("\n".join(lines) + "\n")
    stub.chmod(0o755)
    return stub


def tag_newer_release_content(
    repo: Path, *, removed_file: str | None = None, code_path: str = "libs/host_backup"
) -> None:
    """Commit newer backup code on a side branch and tag it ``minds-v2.0.0``.

    HEAD (main) then reads as *outdated* relative to the tag. ``removed_file``
    additionally deletes that path inside the release commit, for exercising
    convergence onto a tag that removed a file. ``code_path`` is the repo-relative
    backup-service directory (pass ``system/services/host_backup`` for a repo shaped
    like the creation-rename template).
    """
    run_git_for_backup_test(repo, "checkout", "-q", "-b", "release")
    if removed_file is not None:
        run_git_for_backup_test(repo, "rm", "-q", removed_file)
    (repo / code_path / "service.py").write_text("VERSION = 2\n")
    run_git_for_backup_test(repo, "add", "-A")
    run_git_for_backup_test(repo, "commit", "-q", "-m", "release content")
    run_git_for_backup_test(repo, "tag", "minds-v2.0.0")
    run_git_for_backup_test(repo, "checkout", "-q", "main")


def tag_cross_layout_release_content(repo: Path, *, workspace_code_path: str, tag_code_path: str) -> None:
    """Commit newer backup code at the *other* declutter-era path and tag it ``minds-v2.0.0``.

    The release commit moves the backup-service directory from
    ``workspace_code_path`` to ``tag_code_path`` before writing the newer
    content, so the tag's tree carries the code only at ``tag_code_path`` --
    the shape of a pre-declutter tag seen from a decluttered workspace (or
    the reverse). HEAD (main) keeps the workspace layout and reads as
    *outdated* relative to the tag.
    """
    run_git_for_backup_test(repo, "checkout", "-q", "-b", "release")
    (repo / tag_code_path).parent.mkdir(parents=True, exist_ok=True)
    run_git_for_backup_test(repo, "mv", workspace_code_path, tag_code_path)
    (repo / tag_code_path / "service.py").write_text("VERSION = 2\n")
    run_git_for_backup_test(repo, "add", "-A")
    run_git_for_backup_test(repo, "commit", "-q", "-m", "release content")
    run_git_for_backup_test(repo, "tag", "minds-v2.0.0")
    run_git_for_backup_test(repo, "checkout", "-q", "main")


# The sync e2e release tests (apps/minds/test_sync_e2e.py) run in the
# minds-snapshot offload sandbox against a real per-run CI connector env.
# The env's coordinates + admin secrets are forwarded into the sandbox as
# env vars (only on run_minds_release_tests CI runs); these helpers hold the
# contract in one place so the conftest fixture and any future consumer agree.

SYNC_E2E_CONNECTOR_URL_ENV: Final[str] = "MINDS_SYNC_E2E_CONNECTOR_URL"
SYNC_E2E_LITELLM_URL_ENV: Final[str] = "MINDS_SYNC_E2E_LITELLM_URL"
SYNC_E2E_SUPERTOKENS_URI_ENV: Final[str] = "MINDS_SYNC_E2E_SUPERTOKENS_CONNECTION_URI"
SYNC_E2E_SUPERTOKENS_API_KEY_ENV: Final[str] = "MINDS_SYNC_E2E_SUPERTOKENS_API_KEY"


class SyncE2EEnv(FrozenModel):
    """Coordinates + admin secrets of the real connector env the sync e2e tests target."""

    connector_url: str = Field(description="Base URL of the deployed remote_service_connector")
    litellm_proxy_url: str = Field(description="Base URL of the deployed litellm proxy")
    supertokens_connection_uri: SecretStr = Field(description="SuperTokens core URI for admin user provisioning")
    supertokens_api_key: SecretStr = Field(description="SuperTokens core admin api-key")


class SyncE2EAccount(FrozenModel):
    """A per-test, pre-verified, paid account on the sync e2e connector env."""

    email: str = Field(description="Unique per-test address under the env's seeded paid domain")
    password: SecretStr = Field(description="The account's sign-in password (typed into the real UI)")
    user_id: str = Field(description="SuperTokens user id (used for teardown and record assertions)")
    access_token: SecretStr = Field(description="A session JWT for read-only connector polling from the test")


# The per-cloud release tests (apps/minds/test_*_workspace_release.py) create a
# real VM through a minds-shaped provider block and assert the agent container
# has the shape minds relies on: a gVisor (runsc) sandbox, /run and /tmp on
# tmpfs, and a memory cap derived from the VM's RAM. Each test differs only in
# its provider block and credentials; the mngr driving and the assertions are
# shared here so the per-cloud tests cannot drift.

# Disable every provider a minds profile might otherwise enable, so a release
# test's ``mngr list`` fans out to its one cloud provider only.
_CLOUD_WORKSPACE_RELEASE_TEST_DISABLED_PROVIDERS: Final[str] = (
    "\n[providers.modal]\nis_enabled = false\n"
    "\n[providers.vultr]\nis_enabled = false\n"
    "\n[providers.ovh]\nis_enabled = false\n"
    "\n[providers.imbue_cloud]\nis_enabled = false\n"
    "\n[providers.aws]\nis_enabled = false\n"
    "\n[providers.gcp]\nis_enabled = false\n"
    "\n[providers.azure]\nis_enabled = false\n"
)

# Hard timeout for one mngr command in a cloud release test; a create that
# provisions a VM and builds the container routinely runs several minutes.
_CLOUD_WORKSPACE_MNGR_COMMAND_TIMEOUT_SECONDS: Final[int] = 600


def make_cloud_workspace_release_env(
    tmp_path: Path, provider_block: str, base_env: Mapping[str, str]
) -> dict[str, str]:
    """The subprocess env a cloud workspace release test drives mngr with.

    Writes a self-contained project ``settings.toml`` under ``tmp_path`` holding
    ``provider_block`` (the cloud's ``[providers.<name>]`` block, written exactly
    as minds writes it plus the release-test-only ``auto_shutdown_seconds`` safety
    net the cloud providers require under pytest) with every other provider
    disabled, and points ``MNGR_PROJECT_CONFIG_DIR`` at it. ``MNGR_HOST_DIR`` and
    ``HOME`` are isolated under ``tmp_path`` so no developer mngr profile or config
    is loaded (every loaded config must opt into pytest, and a developer's would
    not); ``base_env`` supplies everything else, such as the cloud credentials.
    """
    settings_dir = tmp_path / "config"
    settings_dir.mkdir()
    (settings_dir / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n" + provider_block + _CLOUD_WORKSPACE_RELEASE_TEST_DISABLED_PROVIDERS
    )
    env = dict(base_env)
    env["MNGR_PROJECT_CONFIG_DIR"] = str(settings_dir)
    env["MNGR_HOST_DIR"] = str(tmp_path / "mngr_home")
    env["HOME"] = str(tmp_path / "home")
    Path(env["HOME"]).mkdir()
    return env


def run_mngr_for_cloud_workspace_release_test(
    env: Mapping[str, str], cwd: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    """Run the monorepo's ``mngr`` (the dev shim on PATH) with the release env in scope.

    Invokes the bare ``mngr`` shim rather than ``uv run mngr``: ``uv run`` in an
    arbitrary cwd would try to build that directory's own venv, whereas the shim
    always routes to this checkout's mngr. Streams stdout+stderr to a file so a
    stuck create is still diagnosable on timeout. The log is written *outside*
    ``cwd`` so it doesn't dirty the source git repo (``mngr create`` enforces a
    clean working tree).
    """
    log_path = cwd.parent / f"mngr-{args[0] if args else 'cmd'}.log"
    with log_path.open("w") as log_file:
        proc = subprocess.Popen(
            ["mngr", *args],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(cwd),
            env=env,
        )
        try:
            returncode = proc.wait(timeout=_CLOUD_WORKSPACE_MNGR_COMMAND_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            returncode = 124
    return subprocess.CompletedProcess(args=list(args), returncode=returncode, stdout=log_path.read_text(), stderr="")


def assert_cloud_workspace_container_shape(
    env: Mapping[str, str], repo: Path, *, agent_address: str, host_name: str, provider_name: str
) -> None:
    """Assert a created cloud workspace has the container shape minds relies on.

    The host lists as running on its provider; the agent runs inside a gVisor
    (runsc) sandbox (gVisor advertises itself in the emulated kernel log and in
    /proc/version, a normal kernel does neither, and either also proves the agent
    is containerized rather than on the bare VM); /run and /tmp are tmpfs (the
    mounts runsc needs for supervisord's control socket), /tmp executable and
    capped at the mngr_vps fraction of the VM's RAM; and the container's memory
    is capped at the VM's RAM minus the mngr_vps reserve.
    """
    listing = run_mngr_for_cloud_workspace_release_test(env, repo, "list")
    assert listing.returncode == 0, f"list failed:\n{listing.stdout}"
    assert host_name in listing.stdout
    assert provider_name in listing.stdout

    inner_probe = run_mngr_for_cloud_workspace_release_test(
        env,
        repo,
        "exec",
        agent_address,
        "cat /proc/version; echo '---dmesg---'; dmesg 2>/dev/null | head -5; echo '---mounts---'; mount | grep -E ' /(run|tmp) '",
    )
    assert inner_probe.returncode == 0, f"exec failed:\n{inner_probe.stdout}"
    assert "gvisor" in inner_probe.stdout.lower(), (
        f"expected a gVisor (runsc) signature in /proc/version or dmesg, got:\n{inner_probe.stdout}"
    )
    assert " /run type tmpfs " in inner_probe.stdout, f"expected /run on tmpfs, got:\n{inner_probe.stdout}"
    tmp_mount_lines = [line for line in inner_probe.stdout.splitlines() if " /tmp type tmpfs " in line]
    assert len(tmp_mount_lines) == 1, f"expected /tmp on tmpfs, got:\n{inner_probe.stdout}"
    assert "noexec" not in tmp_mount_lines[0], f"expected an executable /tmp, got:\n{tmp_mount_lines[0]}"

    outer_probe = run_mngr_for_cloud_workspace_release_test(
        env,
        repo,
        "exec",
        "--outer",
        agent_address,
        "awk '/^MemTotal:/{print $2}' /proc/meminfo; "
        f"docker inspect --format '{{{{.HostConfig.Memory}}}}' $(docker ps -q --filter label={LABEL_HOST_ID})",
    )
    assert outer_probe.returncode == 0, f"outer exec failed:\n{outer_probe.stdout}"
    probe_lines = [line.strip() for line in outer_probe.stdout.splitlines() if line.strip().isdigit()]
    assert len(probe_lines) == 2, f"expected MemTotal and the container memory cap, got:\n{outer_probe.stdout}"
    mem_total_kib, container_memory_bytes = (int(line) for line in probe_lines)
    assert container_memory_bytes == expected_container_memory_cap_bytes(mem_total_kib), (
        f"container memory cap {container_memory_bytes} does not match the VM's RAM ({mem_total_kib} KiB)"
    )
    expected_tmp_size_option = f"size={container_tmp_tmpfs_size_mib(mem_total_kib) * 1024}k"
    assert expected_tmp_size_option in tmp_mount_lines[0], (
        f"expected /tmp capped at {expected_tmp_size_option} for a VM with {mem_total_kib} KiB, got:\n{tmp_mount_lines[0]}"
    )


MINDS_APP_DIR: Final[Path] = Path(__file__).resolve().parents[2]


class MindsInstall(FrozenModel):
    """One way of installing minds, named by the pyproject whose dependencies decide what it carries."""

    test_id: str = Field(description="The pytest id for tests parametrized over installs")
    description: str = Field(description="How the install is named in failure messages")
    pyproject_path: Path = Field(description="The pyproject whose [project.dependencies] the install carries")
    fix_instructions: str = Field(description="How to add a missing package to this install")


MINDS_INSTALLS: Final[tuple[MindsInstall, ...]] = (
    MindsInstall(
        test_id="packaged_app",
        description="the packaged desktop app",
        pyproject_path=MINDS_APP_DIR / "electron" / "pyproject" / "pyproject.toml",
        fix_instructions=(
            "add it to ALL FOUR mirrored workspace-package lists: "
            "apps/minds/scripts/build.js (WORKSPACE_PACKAGES), apps/minds/electron/env-setup.js "
            "(WORKSPACE_PACKAGES), apps/minds/electron/pyproject/pyproject.toml "
            "([project.dependencies] AND [tool.uv.sources], then run `uv lock` in that directory), "
            "and apps/minds/scripts/build_test.py (WORKSPACE_PACKAGES)."
        ),
    ),
    MindsInstall(
        test_id="source_install",
        description="minds run from source",
        pyproject_path=MINDS_APP_DIR / "pyproject.toml",
        fix_instructions=(
            "add it to apps/minds/pyproject.toml ([project.dependencies] AND [tool.uv.sources]), "
            "then run `uv lock` at the repo root and in apps/minds/electron/pyproject"
            "."
        ),
    ),
)


def read_pyproject_dependency_names(pyproject_path: Path) -> frozenset[str]:
    """The lowercased distribution names in a pyproject's ``[project.dependencies]``."""
    dependencies = tomllib.loads(pyproject_path.read_text())["project"]["dependencies"]
    return frozenset(
        re.split(r"[><=!~\[; ]", dependency, maxsplit=1)[0].strip().lower() for dependency in dependencies
    )


def get_mngr_plugin_package_names() -> dict[str, str]:
    """Map each installed mngr plugin's entry-point name to its distribution, e.g. ``pi_coding`` -> ``imbue-mngr-pi-coding``."""
    return {
        entry_point.name: entry_point.dist.metadata["Name"]
        for entry_point in importlib_metadata.entry_points(group="mngr")
        if entry_point.dist is not None
    }


# The docker provider keeps a singleton ``*docker-state*`` sidecar container per
# workspace; the workspace agent container is the one that is NOT the
# docker-state sidecar.
_DOCKER_STATE_CONTAINER_MARKER: Final[str] = "docker-state"
# Where the workspace's repo checkout lives inside its container, and where
# every in-workspace command the app runs starts.
WORKSPACE_CONTAINER_ROOT: Final[str] = "/home/user/workspace"
# system_interface's in-container port. It is a core bootstrap-managed
# app with a fixed port (registered in the data/.state app registry);
# kept as a constant so a drift shows up as a clear assertion failure.
SYSTEM_INTERFACE_PORT: Final[int] = 8000
MNGR_START_TIMEOUT_SECONDS: Final[int] = 300
_SYSTEM_INTERFACE_READY_TIMEOUT_SECONDS: Final[int] = 120


class RunningWorkspaceContainer(FrozenModel):
    """A running workspace container built from the default workspace template, and its system-services agent."""

    container_name: str = Field(description="The workspace's Docker container (not its docker-state sidecar)")
    services_agent_id: str = Field(description="The id of the primary system-services agent")


def snapshot_workspace_container_prefix() -> str:
    """The container-name prefix of the minds env under test.

    mngr names docker hosts ``{mngr_prefix}-{host_name}`` and the sandbox's
    conftest pins ``MINDS_ROOT_NAME`` to the snapshot's root, so in CI this is
    the ``minds-ci-snapshot-`` prefix every baked container carries. Scoping by
    the env (rather than a bare ``minds-``) is what makes these tests runnable
    on a dev machine, where containers from many envs coexist: only the named
    env's containers are started or selected, never a colleague env's.
    """
    return f"{os.environ.get('MINDS_ROOT_NAME', CI_SNAPSHOT_MINDS_ROOT_NAME)}-"


def format_container_states(states: Mapping[str, DockerContainerState]) -> str:
    """Render container states for an assertion message, sorted so the text is stable."""
    return ", ".join(f"{name}={state.value}" for name, state in sorted(states.items()))


def _recorded_env_container_names(snapshot_container_states: Mapping[str, DockerContainerState]) -> list[str]:
    """The containers under the env's prefix that the snapshot was resumed with."""
    prefix = snapshot_workspace_container_prefix()
    return sorted(name for name in snapshot_container_states if name.startswith(prefix))


def snapshot_workspace_container_name(snapshot_container_states: Mapping[str, DockerContainerState]) -> str:
    """The workspace container (not the docker-state sidecar) the snapshot was resumed with.

    Read from the snapshot's recording rather than from the daemon: offload reuses a sandbox
    across batches, so workspaces other tests created can be running beside it under the same prefix.
    """
    workspace_names = [
        name
        for name in _recorded_env_container_names(snapshot_container_states)
        if _DOCKER_STATE_CONTAINER_MARKER not in name
    ]
    assert len(workspace_names) == 1, (
        f"Expected exactly one {snapshot_workspace_container_prefix()}* workspace container among those the "
        f"snapshot was resumed with: {format_container_states(snapshot_container_states)}"
    )
    return workspace_names[0]


def start_snapshot_containers(snapshot_container_states: Mapping[str, DockerContainerState]) -> None:
    """Start the snapshot's workspace and docker-state containers; already-running ones are left as they are."""
    subprocess.run(
        ["docker", "start", *_recorded_env_container_names(snapshot_container_states)],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )


def exec_in_workspace_container(
    container_name: str, command: str, *, timeout: int
) -> subprocess.CompletedProcess[str]:
    """Run a shell command inside ``container_name`` via ``docker exec``."""
    return subprocess.run(
        ["docker", "exec", container_name, "bash", "-lc", command],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def list_agents_in_workspace_container(container_name: str) -> list[dict[str, Any]]:
    """Return the agents mngr sees from inside the workspace container.

    Run from inside the host (the container), where mngr uses the local
    provider and the baked-in ``MNGR_HOST_DIR=/home/user/.mngr`` -- no desktop-side
    provider fan-out, so an unrelated (uncredentialed) provider can't blank
    the listing. ``--on-error continue`` keeps any single provider failure
    from aborting the list.
    """
    result = exec_in_workspace_container(
        container_name,
        f"cd {WORKSPACE_CONTAINER_ROOT} && mngr list --format json --on-error continue",
        timeout=60,
    )
    assert result.returncode == 0, f"`mngr list` failed inside {container_name}: {result.stderr}"
    return json.loads(result.stdout)["agents"]


def system_services_agent_id(container_name: str) -> str:
    """Return the id of the primary system-services agent (runs the bootstrap)."""
    agents = list_agents_in_workspace_container(container_name)
    for agent in agents:
        if agent.get("labels", {}).get("is_primary") == "true" or agent.get("name") == "system-services":
            return agent["id"]
    raise AssertionError(f"No primary system-services agent among {[a.get('name') for a in agents]!r}")


def wait_for_system_interface_up(container_name: str) -> bool:
    """Poll system_interface from inside the container until it answers, or time out.

    The poll loop (and its sleeps) run in shell inside ``docker exec`` rather
    than Python so the test never calls ``time.sleep``. Any 2xx/3xx/401 means
    the interface is serving (it may redirect to auth); a connection refused
    surfaces as ``000``.
    """
    poll = (
        "for i in $(seq 1 40); do "
        f"code=$(curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{SYSTEM_INTERFACE_PORT}/ 2>/dev/null); "
        'case "$code" in 200|301|302|307|401) exit 0;; esac; '
        "sleep 3; done; exit 1"
    )
    return (
        exec_in_workspace_container(
            container_name, poll, timeout=_SYSTEM_INTERFACE_READY_TIMEOUT_SECONDS + 30
        ).returncode
        == 0
    )


def resume_snapshot_workspace(
    snapshot_container_states: Mapping[str, DockerContainerState],
) -> RunningWorkspaceContainer:
    """Resume the minds snapshot's workspace container and return it once system_interface serves.

    The captured container is stopped, so a sandbox booted from the snapshot
    must (1) ``docker start`` it and (2) restart the system-services agent so
    the bootstrap respawns system_interface. This is the mngr-level building
    block behind minds' own recovery flow.
    """
    container_name = snapshot_workspace_container_name(snapshot_container_states)
    start_snapshot_containers(snapshot_container_states)
    services_agent_id = system_services_agent_id(container_name)
    start_result = exec_in_workspace_container(
        container_name,
        f"cd {WORKSPACE_CONTAINER_ROOT} && mngr start {services_agent_id} --quiet",
        timeout=MNGR_START_TIMEOUT_SECONDS,
    )
    assert start_result.returncode == 0, f"`mngr start` failed for system-services: {start_result.stderr}"
    assert wait_for_system_interface_up(container_name), (
        "system_interface never answered after resuming the system-services agent."
    )
    return RunningWorkspaceContainer(container_name=container_name, services_agent_id=services_agent_id)


def run_workspace_program_in_container(
    workspace: RunningWorkspaceContainer, program: ModuleType, args: Sequence[str], *, timeout: int
) -> subprocess.CompletedProcess[str]:
    """Run a workspace program at the workspace root, through the same command the app ships it with."""
    command = build_workspace_program_command(program, args)
    return exec_in_workspace_container(
        workspace.container_name, f"cd {WORKSPACE_CONTAINER_ROOT} && {command}", timeout=timeout
    )
