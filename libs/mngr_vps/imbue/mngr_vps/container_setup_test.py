import os
import shutil
from pathlib import Path
from typing import Any
from typing import cast

import pytest
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import DockerBuilder
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.testing import run_git_command
from imbue.mngr_vps.container_setup import _clone_build_context_for_self_contained_git
from imbue.mngr_vps.container_setup import _raise_if_cwd_deleted_for_relative_context
from imbue.mngr_vps.container_setup import build_home_volume_symlink_command
from imbue.mngr_vps.container_setup import build_image_on_outer
from imbue.mngr_vps.container_setup import build_image_on_outer_from_build_args
from imbue.mngr_vps.container_setup import build_write_container_file_command
from imbue.mngr_vps.container_setup import container_tmp_tmpfs_size_mib
from imbue.mngr_vps.container_setup import env_sourced_build_secrets
from imbue.mngr_vps.container_setup import has_memory_limit_start_arg
from imbue.mngr_vps.container_setup import has_restart_policy_start_arg
from imbue.mngr_vps.container_setup import image_exists
from imbue.mngr_vps.container_setup import memory_cap_labels
from imbue.mngr_vps.container_setup import resolve_remote_build_root
from imbue.mngr_vps.container_setup import restart_policy_start_args
from imbue.mngr_vps.container_setup import runsc_tmpfs_start_args
from imbue.mngr_vps.container_setup import tmpfs_mount_paths_in_start_args
from imbue.mngr_vps.data_types import ContainerFile


def test_tmpfs_mount_paths_in_start_args_reads_both_flag_spellings() -> None:
    args = ("--tmpfs", "/run", "--tmpfs=/tmp:rw,size=64m", "--workdir=/", "--tmpfs", "/scratch:noexec")
    assert tmpfs_mount_paths_in_start_args(args) == {"/run", "/tmp", "/scratch"}
    assert tmpfs_mount_paths_in_start_args(()) == set()
    assert tmpfs_mount_paths_in_start_args(("--memory=1g", "--restart=unless-stopped")) == set()


# An 8 GB cloud VM reports about 7945 MiB of MemTotal, so its /tmp cap is 993 MiB.
_EIGHT_GB_VM_MEM_TOTAL_KIB = 8136000


def test_container_tmp_tmpfs_size_is_an_eighth_of_the_vms_ram_with_a_floor() -> None:
    assert container_tmp_tmpfs_size_mib(_EIGHT_GB_VM_MEM_TOTAL_KIB) == 993
    assert container_tmp_tmpfs_size_mib(16 * 1024 * 1024) == 2048
    assert container_tmp_tmpfs_size_mib(4 * 1024 * 1024) == 512
    # A 2 GB VM would get 256 MiB from the fraction alone; the floor holds it there too.
    assert container_tmp_tmpfs_size_mib(1990000) == 256
    assert container_tmp_tmpfs_size_mib(900000) == 256


def test_runsc_tmpfs_start_args_only_under_runsc_and_only_for_missing_mounts() -> None:
    assert runsc_tmpfs_start_args(None, (), _EIGHT_GB_VM_MEM_TOTAL_KIB) == ()
    assert runsc_tmpfs_start_args("runc", (), _EIGHT_GB_VM_MEM_TOTAL_KIB) == ()
    assert runsc_tmpfs_start_args("runsc", (), _EIGHT_GB_VM_MEM_TOTAL_KIB) == (
        "--tmpfs",
        "/run",
        "--tmpfs",
        "/tmp:exec,size=993m",
    )
    assert runsc_tmpfs_start_args("runsc", ("--tmpfs", "/run"), _EIGHT_GB_VM_MEM_TOTAL_KIB) == (
        "--tmpfs",
        "/tmp:exec,size=993m",
    )
    # A caller's own /tmp mount (the gen-2 slice args) is kept as it is, unsized or not.
    assert runsc_tmpfs_start_args("runsc", ("--tmpfs=/run", "--tmpfs=/tmp:exec"), _EIGHT_GB_VM_MEM_TOTAL_KIB) == ()
    assert runsc_tmpfs_start_args("runsc", ("--tmpfs", "/tmp:exec,size=1g"), _EIGHT_GB_VM_MEM_TOTAL_KIB) == (
        "--tmpfs",
        "/run",
    )


def test_has_memory_limit_start_arg_reads_every_docker_spelling() -> None:
    assert has_memory_limit_start_arg(("--memory=4096m",))
    assert has_memory_limit_start_arg(("--memory", "4g"))
    assert has_memory_limit_start_arg(("--cpus=2", "-m", "4g"))
    assert not has_memory_limit_start_arg(())
    assert not has_memory_limit_start_arg(("--memory-swap=4g", "--restart=unless-stopped"))


def test_memory_cap_labels_mark_only_containers_without_an_explicit_limit() -> None:
    assert memory_cap_labels(("--restart=unless-stopped",)) == {"com.imbue.mngr.memory-cap": "vm"}
    assert memory_cap_labels(("--memory=7168m", "--memory-swap=7168m")) == {}


def test_restart_policy_start_args_defaults_to_unless_stopped_unless_the_caller_chose_one() -> None:
    assert restart_policy_start_args(()) == ("--restart=unless-stopped",)
    assert restart_policy_start_args(("--workdir=/",)) == ("--restart=unless-stopped",)
    assert restart_policy_start_args(("--restart=unless-stopped",)) == ()
    assert restart_policy_start_args(("--restart", "always")) == ()
    assert restart_policy_start_args(("--restart=no",)) == ()


def test_has_restart_policy_start_arg_reads_both_flag_spellings() -> None:
    assert has_restart_policy_start_arg(("--restart", "on-failure:3"))
    assert has_restart_policy_start_arg(("--restart=always",))
    assert not has_restart_policy_start_arg(("--restart-not-a-flag", "--workdir=/"))


class _ImageInspectOuter(MutableModel):
    """Outer host that succeeds only for ``docker image inspect`` of a known-present image."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    present_image: str

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Any = None,
        env: Any = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        is_inspect_of_present = "image inspect" in command and self.present_image in command
        if is_inspect_of_present:
            return CommandResult(stdout="[{}]", stderr="", success=True)
        return CommandResult(stdout="", stderr="No such image", success=False)


def test_image_exists_true_when_inspect_succeeds() -> None:
    outer = cast(OuterHostInterface, _ImageInspectOuter(present_image="default-workspace-template:minds-v9.9.9"))
    assert image_exists(outer, "default-workspace-template:minds-v9.9.9") is True


def test_image_exists_false_when_inspect_fails() -> None:
    outer = cast(OuterHostInterface, _ImageInspectOuter(present_image="default-workspace-template:minds-v9.9.9"))
    assert image_exists(outer, "default-workspace-template:absent-tag") is False


class _FixedResultOuter(MutableModel):
    """Outer host that answers every command with the same result and records what was asked."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    result: CommandResult
    issued_commands: list[str] = Field(default_factory=list)

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Any = None,
        env: Any = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.issued_commands.append(command)
        return self.result


def test_resolve_remote_build_root_lives_under_dockers_data_root() -> None:
    outer = _FixedResultOuter(result=CommandResult(stdout="/var/lib/docker\n", stderr="", success=True))
    assert resolve_remote_build_root(cast(OuterHostInterface, outer)) == "/var/lib/docker/mngr-build"
    assert outer.issued_commands == ["docker info -f '{{.DockerRootDir}}'"]


def test_resolve_remote_build_root_raises_when_docker_cannot_answer_or_answers_nonsense() -> None:
    for result in (
        CommandResult(stdout="", stderr="Cannot connect to the Docker daemon", success=False),
        CommandResult(stdout="\n", stderr="", success=True),
        CommandResult(stdout="template parsing error\n", stderr="", success=True),
    ):
        with pytest.raises(MngrError, match="docker's data root"):
            resolve_remote_build_root(cast(OuterHostInterface, _FixedResultOuter(result=result)))


def test_clone_build_context_returns_none_for_non_git_context(tmp_path: Path) -> None:
    """A non-git context with no --git-depth is uploaded verbatim (no clone)."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "Dockerfile").write_text("FROM scratch\n")
    assert _clone_build_context_for_self_contained_git(plain, git_depth=None) is None


@pytest.mark.rsync
def test_clone_build_context_drops_worktree_admin_from_primary_checkout(temp_git_repo: Path) -> None:
    """A primary checkout with linked worktrees clones to a self-contained .git.

    Regression test for the AWS create-template: when ``mngr create`` is run
    from a primary checkout that has per-branch linked worktrees, the raw
    ``.git/worktrees/`` admin would otherwise be baked into the image. There it
    marks the operator's other branches as checked out, which makes the
    post-build mirror seed push fail with "refusing to update checked out
    branch" (``git init --bare`` on the target can't release a branch held by a
    linked worktree). A fresh clone has no linked worktrees at all -- the
    structural property asserted here -- so the seed can update every branch.
    The clone must still carry the operator's uncommitted edits.
    """
    # temp_git_repo is a primary checkout on `main` with an initial commit. Give
    # it two extra branches checked out in linked worktrees, mirroring an
    # operator who keeps a worktree per branch (the bug repro).
    primary = temp_git_repo
    for branch in ("mngr/feat-a", "mngr/feat-b"):
        run_git_command(primary, "branch", branch)
        run_git_command(primary, "worktree", "add", str(primary.parent / f"wt-{branch.replace('/', '-')}"), branch)
    # An uncommitted edit that must survive into the build context.
    (primary / "dirty.txt").write_text("in-flight\n")
    # Precondition: the raw checkout carries the worktree admin that breaks the seed.
    assert (primary / ".git" / "worktrees").is_dir()

    clone = _clone_build_context_for_self_contained_git(primary, git_depth=None)
    assert clone is not None
    try:
        # The clone is a standalone repo with no linked worktrees, so no branch
        # is held checked-out by a worktree the seed push can't release.
        assert (clone / ".git").is_dir()
        assert not (clone / ".git" / "worktrees").exists()
        assert run_git_command(clone, "worktree", "list").stdout.strip().count("\n") == 0
        # ...and it still carries the operator's uncommitted edit.
        assert (clone / "dirty.txt").read_text() == "in-flight\n"
    finally:
        # The helper allocates the clone under a fresh tempfile dir; clean it up.
        shutil.rmtree(clone.parent, ignore_errors=True)


def _delete_own_cwd(doomed: Path) -> None:
    """Chdir into ``doomed`` and unlink it, leaving this process with a dead cwd.

    This is what a create attempt experiences when another attempt removes the
    scratch clone it is running from.
    """
    os.chdir(doomed)
    shutil.rmtree(doomed)


def test_relative_build_context_with_deleted_cwd_raises_legible_error(tmp_path: Path) -> None:
    """The exact failure a create attempt hits when its scratch clone is deleted."""
    doomed = tmp_path / "minds-clone-dwt"
    doomed.mkdir()
    original_cwd = os.getcwd()
    try:
        _delete_own_cwd(doomed)
        # Precondition: the unlinked cwd still stats as present, which is why the
        # caller's is-it-a-path filter waves "." through instead of catching this.
        assert Path(".").exists()
        with pytest.raises(MngrError, match="working directory no longer exists"):
            _raise_if_cwd_deleted_for_relative_context(("--file=system/Dockerfile", "."))
    finally:
        os.chdir(original_cwd)


def test_absolute_build_context_survives_a_deleted_cwd(tmp_path: Path) -> None:
    """An absolute context needs no cwd, so the guard must not block it."""
    doomed = tmp_path / "minds-clone-dwt"
    doomed.mkdir()
    original_cwd = os.getcwd()
    try:
        _delete_own_cwd(doomed)
        _raise_if_cwd_deleted_for_relative_context(("--file=system/Dockerfile", str(tmp_path)))
        # And Path.resolve() on an absolute path really does work without a cwd --
        # the premise that it "calls os.getcwd() even for an absolute path" is false.
        assert Path(str(tmp_path)).resolve() == tmp_path.resolve()
    finally:
        os.chdir(original_cwd)


def test_relative_build_context_with_live_cwd_is_allowed(tmp_path: Path) -> None:
    original_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        _raise_if_cwd_deleted_for_relative_context(("--file=system/Dockerfile", "."))
    finally:
        os.chdir(original_cwd)


def test_build_image_on_outer_checks_the_cwd_before_touching_anything(tmp_path: Path) -> None:
    """The guard is wired into the real entry point, not just defined next to it.

    PR #217 added a correct-looking guard to this function that no input ever
    reached; nothing failed, because nothing called it with a real value. This
    test drives the public function so a future refactor cannot orphan the check.
    ``outer``/``cg``/``builder`` are None: reaching them at all means the guard
    did not fire first.
    """
    doomed = tmp_path / "minds-clone-dwt"
    doomed.mkdir()
    original_cwd = os.getcwd()
    try:
        _delete_own_cwd(doomed)
        with pytest.raises(MngrError, match="working directory no longer exists"):
            build_image_on_outer_from_build_args(
                cast(OuterHostInterface, None),
                cast(Any, None),
                host_id=HostId.generate(),
                docker_build_args=("--file=system/Dockerfile", "."),
                git_depth=None,
                builder=cast(Any, None),
            )
    finally:
        os.chdir(original_cwd)


def test_build_home_volume_symlink_command_replaces_a_plain_directory_but_keeps_an_existing_link() -> None:
    command = build_home_volume_symlink_command("/home/user", "/mngr-vol/home")

    assert command == (
        "mkdir -p /mngr-vol/home && ( [ -L /home/user ] || rm -rf /home/user ) && ln -sfn /mngr-vol/home /home/user"
    )


def test_build_write_container_file_command_creates_the_directory_and_quotes_the_content() -> None:
    command = build_write_container_file_command(
        ContainerFile(path="/etc/ssh/principals/root", content="mngr-container\n", mode="0644")
    )
    assert command.startswith(
        "mkdir -p /etc/ssh/principals && printf '%s' 'mngr-container\n' > /etc/ssh/principals/root"
    )
    assert command.endswith("chmod 0644 /etc/ssh/principals/root")
    quoted = build_write_container_file_command(
        ContainerFile(path="/etc/ssh/sshd_config.d/61-mngr-user-ca.conf", content="it's %u\n", mode="0644")
    )
    assert """'it'"'"'s %u\n'""" in quoted


def test_env_sourced_build_secrets_reads_both_flag_spellings_and_skips_unset_names() -> None:
    args = (
        "--file=Dockerfile",
        "--secret",
        "id=a,env=TOKEN_A",
        "--secret=id=b,env=TOKEN_B",
        "--secret=id=c,src=/tmp/c",
    )

    forwarded = env_sourced_build_secrets(args, {"TOKEN_A": "aaa", "UNRELATED": "x"})

    assert forwarded == {"TOKEN_A": "aaa"}


def test_env_sourced_build_secrets_is_empty_without_secret_args() -> None:
    assert env_sourced_build_secrets(("--file=Dockerfile", "."), {"TOKEN_A": "aaa"}) == {}


class _StreamingOuter(MutableModel):
    """Outer host that records the command and env of the one streaming command it runs."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    command: str = ""
    env: dict[str, str] | None = None

    def execute_streaming_command(
        self,
        command: str,
        on_line: Any,
        *,
        env: Any = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.command = command
        self.env = dict(env) if env is not None else None
        return CommandResult(stdout="", stderr="", success=True)


def test_build_image_on_outer_forwards_an_env_sourced_secret_through_the_env_not_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BUILD_TOKEN", "s3cret")
    outer = _StreamingOuter()

    build_image_on_outer(
        cast(OuterHostInterface, outer),
        tag="mngr-build-x",
        build_context_path="/tmp/ctx",
        docker_build_args=("--file=/tmp/ctx/Dockerfile", "--secret=id=tok,env=BUILD_TOKEN"),
        timeout_seconds=5.0,
        on_output=None,
        builder=DockerBuilder.DOCKER,
    )

    assert outer.env == {"BUILD_TOKEN": "s3cret"}
    assert "s3cret" not in outer.command
    assert "--secret=id=tok,env=BUILD_TOKEN" in outer.command


def test_build_image_on_outer_passes_no_env_when_no_secret_is_env_sourced() -> None:
    outer = _StreamingOuter()

    build_image_on_outer(
        cast(OuterHostInterface, outer),
        tag="mngr-build-x",
        build_context_path="/tmp/ctx",
        docker_build_args=("--file=/tmp/ctx/Dockerfile",),
        timeout_seconds=5.0,
        on_output=None,
        builder=DockerBuilder.DOCKER,
    )

    assert outer.env is None
