import base64
import os
import subprocess
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import httpx
import pytest
from loguru import logger
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.bootstrap import mngr_host_dir_for
from imbue.mngr_vps.container_setup import LABEL_HOST_ID
from imbue.mngr_vps.container_setup import container_tmp_tmpfs_size_mib
from imbue.mngr_vps.host_setup import expected_container_memory_cap_bytes

DEFAULT_WORKSPACE_TEMPLATE_OWNER_REPO: Final[str] = "imbue-ai/default-workspace-template"


def fetch_default_workspace_template_file(repo_relative_path: str) -> str | None:
    """Fetch a file from default-workspace-template's default branch via the GitHub contents API.

    The repo is public, so no token is needed. Returns None on any fetch or
    decode failure, so the caller can surface a single "could not fetch" assertion.
    """
    url = f"https://api.github.com/repos/{DEFAULT_WORKSPACE_TEMPLATE_OWNER_REPO}/contents/{repo_relative_path}"
    try:
        response = httpx.get(
            url,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "mngr-template-alignment-test"},
            timeout=30.0,
            follow_redirects=True,
        )
        response.raise_for_status()
    except httpx.HTTPError as e:
        logger.trace("fetch of {} failed: {}", url, e)
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
    profile_id = "testprofile"
    (mngr_host_dir / "config.toml").write_text(f'profile = "{profile_id}"\n')
    settings_dir = mngr_host_dir / "profiles" / profile_id
    settings_dir.mkdir(parents=True, exist_ok=True)
    return settings_dir / "settings.toml"


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
) -> Path:
    """Write a stub ``supervisorctl`` into ``stub_bin`` and return its path.

    ``status`` prints the roster in ``status_lines`` (defaulting to a healthy
    ``host-backup`` + ``system_interface``, with ``host-backup`` STOPPED when
    ``is_restart_ok=False``); ``status <name>`` filters it like the real tool.
    Once a ``restart`` has been attempted, ``status`` switches to
    ``status_lines_after_restart`` when given -- so tests can model a machine
    whose service states differ before and after the restore's restart.

    The ``is_restart_ok=False`` variant additionally fails ``restart``, for
    exercising the update script's rollback path and the restore's
    restart-exit-code independence.

    ``call_log_path`` appends each invocation's arguments as one line, so tests
    can assert on service-lifecycle ordering (e.g. ``stop all`` before
    ``restart all``).
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
    lines.append(f'if [ "$1" = "restart" ]; then touch "{restart_marker}"; fi')
    if not is_restart_ok:
        lines.append('if [ "$1" = "restart" ]; then echo "failed" >&2; exit 1; fi')
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
