import base64
import subprocess
from collections.abc import Mapping
from collections.abc import Sequence
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
from imbue.mngr.primitives import HostId
from imbue.mngr_imbue_cloud.errors import BoxImageCacheError
from imbue.mngr_imbue_cloud.providers.slice_provider import SliceVpsDockerProvider
from imbue.mngr_imbue_cloud.providers.slice_provider import SliceVpsDockerProviderConfig
from imbue.mngr_imbue_cloud.providers.slice_provider import _DEFAULT_WORKSPACE_TEMPLATE_BUILD_CODE_DIR
from imbue.mngr_imbue_cloud.providers.slice_provider import _ENV_D_BROWSER_UNIT
from imbue.mngr_imbue_cloud.providers.slice_provider import _GUEST_FIRST_BOOT_TIMEOUT_SECONDS
from imbue.mngr_imbue_cloud.providers.slice_provider import _IMAGE_CACHE_WAIT_ROUNDS
from imbue.mngr_imbue_cloud.providers.slice_provider import _PLAYWRIGHT_CTX_DIR
from imbue.mngr_imbue_cloud.providers.slice_provider import container_ca_trust_files
from imbue.mngr_imbue_cloud.providers.slice_provider import read_container_ca_trust_files_from_vm
from imbue.mngr_imbue_cloud.providers.slice_provider import render_remove_authorized_key_command
from imbue.mngr_imbue_cloud.providers.slice_provider import resolve_slice_ssh_authority
from imbue.mngr_imbue_cloud.providers.slice_provider import wait_for_guest_cloud_init_to_finish
from imbue.mngr_imbue_cloud.slices.box_image_cache import BoxImageCacheInterface
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_PUBLIC_KEY_PATH
from imbue.mngr_imbue_cloud.slices.mock_box_image_cache_test import MockBoxImageCache

_TAG = "default-workspace-template:minds-v0.3.2"


class _OrchestrationProvider(SliceVpsDockerProvider):
    """Slice provider whose box cache + seed/load steps are recorded, to test the decision branching.

    Overrides the three seams ``_ensure_cached_image_present`` drives -- the cache
    factory and the seed/load actions -- so the branch logic can be exercised
    without a real box, slice dockerd, or image build.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    test_cache: MockBoxImageCache = Field(description="Injected in-memory cache")
    seeded_tags: list[str] = Field(default_factory=list)
    loaded_tags: list[str] = Field(default_factory=list)

    def _make_box_image_cache(self) -> BoxImageCacheInterface:
        return self.test_cache

    def _seed_box_image(
        self,
        *,
        cache: BoxImageCacheInterface,
        outer: OuterHostInterface,
        host_id: HostId,
        vm_ssh_port: int,
        image_tag: str,
        build_args: Sequence[str] | None,
    ) -> None:
        self.seeded_tags.append(image_tag)

    def _load_cached_image(
        self, *, cache: BoxImageCacheInterface, outer: OuterHostInterface, vm_ssh_port: int, image_tag: str
    ) -> None:
        self.loaded_tags.append(image_tag)


def _provider(cache: MockBoxImageCache) -> _OrchestrationProvider:
    # model_construct skips the heavy VpsProvider field wiring; the overridden seams
    # the test exercises touch only the injected cache + the record lists.
    return _OrchestrationProvider.model_construct(test_cache=cache, seeded_tags=[], loaded_tags=[])


def _ensure(provider: _OrchestrationProvider) -> None:
    provider._ensure_cached_image_present(
        outer=_FAKE_OUTER,
        host_id=HostId.generate(),
        vm_ssh_port=2200,
        image_tag=_TAG,
        build_args=("--file=Dockerfile", "."),
    )


# Never used by the overridden seed/load seams; passed only to satisfy the signature.
_FAKE_OUTER: Any = object()


def test_loads_when_tar_already_present() -> None:
    provider = _provider(MockBoxImageCache(tars_present={_TAG}))
    _ensure(provider)
    assert provider.loaded_tags == [_TAG]
    assert provider.seeded_tags == []


def test_seeds_when_no_tar_and_lock_is_acquired() -> None:
    provider = _provider(MockBoxImageCache())
    _ensure(provider)
    assert provider.seeded_tags == [_TAG]
    assert provider.loaded_tags == []


def test_waits_then_loads_when_another_slice_is_seeding() -> None:
    # No tar yet and the lock is held by an in-flight builder, so we must take the
    # try_acquire (fails) -> wait_for_tar (tar appears) -> load path rather than the
    # has_tar() fast path.
    cache = MockBoxImageCache(locks_held={_TAG}, tar_published_after_wait_count=1)
    provider = _provider(cache)
    _ensure(provider)
    assert provider.loaded_tags == [_TAG]
    assert provider.seeded_tags == []


def test_loads_when_the_tar_publishes_during_the_final_wait_round() -> None:
    # A tar published during an earlier round's wait is caught by the next round's
    # has_tar re-check, but one landing during the LAST round's wait has no
    # following round: the post-loop check must load it rather than raising.
    cache = MockBoxImageCache(locks_held={_TAG}, tar_published_after_wait_count=_IMAGE_CACHE_WAIT_ROUNDS)
    provider = _provider(cache)
    _ensure(provider)
    assert provider.loaded_tags == [_TAG]
    assert provider.seeded_tags == []


def test_raises_when_lock_held_and_tar_never_appears() -> None:
    # Lock held by a builder, but the tar never materializes within the wait budget.
    cache = MockBoxImageCache(locks_held={_TAG})
    provider = _provider(cache)
    with pytest.raises(BoxImageCacheError):
        _ensure(provider)
    assert provider.seeded_tags == []
    assert provider.loaded_tags == []


def test_reacquires_and_seeds_when_the_seeder_dies_without_a_tar() -> None:
    # A seeder holds the lock but dies without publishing (its build failed): the
    # wait returns early with the lock released, and the next round must take over
    # as the new seeder instead of stranding until the wait window expires.
    cache = MockBoxImageCache(locks_held={_TAG}, is_lock_released_on_wait=True)
    provider = _provider(cache)
    _ensure(provider)
    assert provider.seeded_tags == [_TAG]
    assert provider.loaded_tags == []


class _RecordingOuter(MutableModel):
    """Outer host that records every command and reports success, for command-render assertions."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    recorded: list[str] = Field(default_factory=list)

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.recorded.append(command)
        return CommandResult(stdout="", stderr="", success=True)


def test_build_playwright_derived_image_renders_marker_and_build_command() -> None:
    provider = SliceVpsDockerProvider.model_construct()
    outer = _RecordingOuter()
    provider._build_playwright_derived_image(
        outer=cast(OuterHostInterface, outer), base_image="mngr-build-xyz", target_tag=_TAG
    )
    # The staged context Dockerfile is shipped base64-encoded; decode it and assert the
    # baked-browser contract every loaded slice relies on (the env.d unit's fast
    # satisfied-check finds the engine already installed).
    stage_command = next(c for c in outer.recorded if "base64 -d" in c)
    encoded = stage_command.split("echo ")[1].split(" | base64 -d")[0].strip().strip("'")
    dockerfile = base64.b64decode(encoded).decode()
    assert dockerfile.startswith("FROM mngr-build-xyz")
    # The bake runs the exact env.d browser unit the workspace runs at boot,
    # pointed at the relocated build tree.
    assert _ENV_D_BROWSER_UNIT in dockerfile
    assert "ENV_CONVERGE_WORKSPACE_DIR=" in dockerfile
    # Guards the DEFAULT_WORKSPACE_TEMPLATE build-code path so a relocated layout fails fast with a clear message.
    assert f"test -d {_DEFAULT_WORKSPACE_TEMPLATE_BUILD_CODE_DIR}" in dockerfile
    # The RUN body must be valid shell -- catches f-string brace-escaping bugs in the guard.
    run_body = next(line for line in dockerfile.splitlines() if line.startswith("RUN "))[len("RUN ") :]
    syntax_check = subprocess.run(["bash", "-n", "-c", run_body], capture_output=True, text=True)
    assert syntax_check.returncode == 0, syntax_check.stderr
    build_command = next(c for c in outer.recorded if "docker build" in c)
    assert _TAG in build_command
    assert f"{_PLAYWRIGHT_CTX_DIR}/Dockerfile" in build_command


def test_transfer_key_authorize_and_deauthorize_render_expected_commands() -> None:
    provider = SliceVpsDockerProvider.model_construct()
    outer = _RecordingOuter()
    public_key = "ssh-ed25519 AAAATESTKEY"
    provider._authorize_transfer_key(cast(OuterHostInterface, outer), public_key)
    provider._deauthorize_transfer_key(cast(OuterHostInterface, outer), public_key)
    authorize_command, deauthorize_command = outer.recorded
    assert ">> /root/.ssh/authorized_keys" in authorize_command
    assert public_key in authorize_command
    assert "grep -vF" in deauthorize_command
    assert public_key in deauthorize_command


def _remove_authorized_key(authorized_keys: Path, public_key: str) -> None:
    """Run the rendered removal against a real file; it must succeed and leave no temp file behind."""
    result = subprocess.run(
        ["bash", "-c", render_remove_authorized_key_command(public_key, str(authorized_keys))],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not authorized_keys.with_name(f"{authorized_keys.name}.tmp").exists()


def test_remove_authorized_key_command_empties_a_file_that_held_only_that_key(tmp_path: Path) -> None:
    # The gen-2 case: VM root authorizes nothing but the bake's transfer key,
    # so removing it leaves grep with nothing to print (exit 1).
    authorized_keys = tmp_path / "authorized_keys"
    transfer_key = "ssh-ed25519 AAAAONLYKEY transfer@box"
    authorized_keys.write_text(f"{transfer_key}\n")

    _remove_authorized_key(authorized_keys, transfer_key)

    assert authorized_keys.read_text() == ""


def test_remove_authorized_key_command_keeps_the_other_authorized_keys(tmp_path: Path) -> None:
    authorized_keys = tmp_path / "authorized_keys"
    transfer_key = "ssh-ed25519 AAAATRANSFER transfer@box"
    kept_key = "ssh-ed25519 AAAAKEPT owner@device"
    authorized_keys.write_text(f"{kept_key}\n{transfer_key}\n")

    _remove_authorized_key(authorized_keys, transfer_key)

    assert authorized_keys.read_text() == f"{kept_key}\n"


def test_extra_start_args_cap_container_memory_from_the_slice_size() -> None:
    # Both container-creation paths (bake and slow-path rebuild) flow through
    # create_host_on_existing_vps, whose extra-start-args seam must hard-cap the
    # container at the slice's RAM minus the VM-side reserve -- swap pinned equal
    # so the workspace is shed under pressure instead of thrashing the VM.
    provider = SliceVpsDockerProvider.model_construct(slice_config=SliceVpsDockerProviderConfig(slice_memory_mib=8192))
    assert provider._compute_extra_start_args() == ("--memory=7168m", "--memory-swap=7168m")


def test_extra_start_args_are_empty_when_the_slice_size_is_unknown() -> None:
    # A rebuild against a legacy lease row without a memory_gb stamp must keep
    # the previous uncapped behavior rather than guessing a cap.
    provider = SliceVpsDockerProvider.model_construct(slice_config=SliceVpsDockerProviderConfig())
    assert provider._compute_extra_start_args() == ()


class _FailingRecordingOuter(_RecordingOuter):
    """Recording outer whose commands report a non-success terminal result (and record their timeouts)."""

    recorded_timeouts: list[float | None] = Field(default_factory=list)

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.recorded.append(command)
        self.recorded_timeouts.append(timeout_seconds)
        return CommandResult(stdout="status: degraded", stderr="", success=False)


def test_guest_cloud_init_wait_blocks_on_first_boot_and_tolerates_a_degraded_state() -> None:
    # The wait is purely a serialization barrier: it must block on cloud-init's
    # own --wait with the first-boot timeout, and a degraded terminal state must
    # not raise (the following provisioning steps validate real functionality).
    outer = _FailingRecordingOuter()
    wait_for_guest_cloud_init_to_finish(cast(OuterHostInterface, outer))
    assert outer.recorded == ["cloud-init status --wait"]
    assert outer.recorded_timeouts == [_GUEST_FIRST_BOOT_TIMEOUT_SECONDS]


_CA_PUBLIC_KEY = "ssh-ed25519 AAAACAKEY minds-dev-ssh-ca"
_POOL_PUBLIC_KEY = "ssh-ed25519 AAAAPOOLKEY pool"


def test_resolve_slice_ssh_authority_gen2_without_a_ca_key_raises() -> None:
    with pytest.raises(MngrError, match="trusted_user_ca_public_key"):
        resolve_slice_ssh_authority(is_gen2=True, trusted_user_ca_public_key=None, pool_authorized_public_key=None)


def test_resolve_slice_ssh_authority_gen2_trusts_the_ca_and_authorizes_no_static_key() -> None:
    authority = resolve_slice_ssh_authority(
        is_gen2=True, trusted_user_ca_public_key=_CA_PUBLIC_KEY, pool_authorized_public_key=_POOL_PUBLIC_KEY
    )
    assert authority.vm_trusted_user_ca_public_key == _CA_PUBLIC_KEY
    assert authority.extra_root_authorized_keys == ()
    assert authority.container_ssh_config_files == container_ca_trust_files(_CA_PUBLIC_KEY)
    assert authority.container_ssh_config_files != ()


def test_resolve_slice_ssh_authority_gen1_authorizes_the_pool_key_and_no_ca_trust() -> None:
    # Even with a CA key also present (the two knobs are meant to be mutually
    # exclusive per generation, but the caller enforces that, not this
    # function): a gen-1 box must never get container CA trust files.
    authority = resolve_slice_ssh_authority(
        is_gen2=False, trusted_user_ca_public_key=_CA_PUBLIC_KEY, pool_authorized_public_key=_POOL_PUBLIC_KEY
    )
    assert authority.vm_trusted_user_ca_public_key is None
    assert authority.extra_root_authorized_keys == (_POOL_PUBLIC_KEY,)
    assert authority.container_ssh_config_files == ()


def test_resolve_slice_ssh_authority_gen1_with_no_pool_key_authorizes_nothing() -> None:
    authority = resolve_slice_ssh_authority(
        is_gen2=False, trusted_user_ca_public_key=None, pool_authorized_public_key=None
    )
    assert authority.vm_trusted_user_ca_public_key is None
    assert authority.extra_root_authorized_keys == ()
    assert authority.container_ssh_config_files == ()


def test_read_container_ca_trust_files_from_vm_returns_empty_when_the_vm_trusts_no_ca() -> None:
    outer = _RecordingOuter()
    assert read_container_ca_trust_files_from_vm(cast(OuterHostInterface, outer)) == ()
    assert outer.recorded == [f"if [ -e {SSH_CA_PUBLIC_KEY_PATH} ]; then cat {SSH_CA_PUBLIC_KEY_PATH}; fi"]


class _CaReportingOuter(_RecordingOuter):
    """Recording outer whose command answers with a fixed CA public key (or a failure)."""

    ca_public_key: str = ""
    is_read_successful: bool = True

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.recorded.append(command)
        if not self.is_read_successful:
            return CommandResult(stdout="", stderr="permission denied", success=False)
        return CommandResult(stdout=self.ca_public_key + "\n", stderr="", success=True)


def test_read_container_ca_trust_files_from_vm_returns_the_trust_files_for_the_vms_pinned_ca() -> None:
    outer = _CaReportingOuter(ca_public_key=_CA_PUBLIC_KEY)
    assert read_container_ca_trust_files_from_vm(cast(OuterHostInterface, outer)) == container_ca_trust_files(
        _CA_PUBLIC_KEY
    )


def test_read_container_ca_trust_files_from_vm_raises_when_the_read_fails() -> None:
    outer = _CaReportingOuter(is_read_successful=False)
    with pytest.raises(MngrError, match="trusted SSH CA"):
        read_container_ca_trust_files_from_vm(cast(OuterHostInterface, outer))
