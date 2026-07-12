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
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr_imbue_cloud.errors import BoxImageCacheError
from imbue.mngr_imbue_cloud.providers.slice_provider import SliceVpsDockerProvider
from imbue.mngr_imbue_cloud.providers.slice_provider import _DEFAULT_WORKSPACE_TEMPLATE_BUILD_CODE_DIR
from imbue.mngr_imbue_cloud.providers.slice_provider import _DEFERRED_INSTALL_MARKER
from imbue.mngr_imbue_cloud.providers.slice_provider import _PLAYWRIGHT_CTX_DIR
from imbue.mngr_imbue_cloud.slices.box_image_cache import BoxImageCacheInterface
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
    cache = MockBoxImageCache(locks_held={_TAG}, is_tar_published_on_wait=True)
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
    # baked-Playwright + deferred-install-marker contract every loaded slice relies on.
    stage_command = next(c for c in outer.recorded if "base64 -d" in c)
    encoded = stage_command.split("echo ")[1].split(" | base64 -d")[0].strip().strip("'")
    dockerfile = base64.b64decode(encoded).decode()
    assert dockerfile.startswith("FROM mngr-build-xyz")
    # Must invoke playwright via ``python -m`` (not the ``playwright`` console script): the DEFAULT_WORKSPACE_TEMPLATE
    # venv is built at /mngr/code and ``mv``\\d to /docker_build_code, so the script's hardcoded
    # shebang is broken here -- only the interpreter (reached via ``python -m``) is relocatable.
    assert "uv run python -m playwright install --with-deps chromium" in dockerfile
    assert _DEFERRED_INSTALL_MARKER in dockerfile
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
